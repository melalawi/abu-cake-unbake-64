"""Source rules and match refusals with explicit, recorded exceptions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unbake.decomp.needs import GuardFinding, Need, register_deriver, register_resolver
from unbake.layout.split import Edit
from unbake.project.config import Held


@dataclass(frozen=True)
class Rule:
    id: str
    applies_to: str
    check: Callable[[str, str], list[GuardFinding]]


_LEX = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
_MARKER = re.compile(r"/\*\s*FAKEMATCH:\s*(.*?)\*/", re.S)


def _code(source: str) -> str:
    return _LEX.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m[0]), source)


def fakematches(source: str) -> tuple[str, ...]:
    """Return explicit exception reasons, suitable for a match receipt."""
    if not isinstance(source, str):
        raise Held("checks", "source_text is missing or invalid")
    comments = [m[0] for m in _LEX.finditer(source) if m[0].startswith("/*")]
    reasons = []
    for comment in comments:
        if "FAKEMATCH:" not in comment:
            continue
        marker = _MARKER.fullmatch(comment)
        if marker is None or not marker[1].strip():
            raise Held("checks", "FAKEMATCH.reason is missing or invalid")
        reasons.append(marker[1].strip())
    return tuple(dict.fromkeys(reasons))


def _finding(rule: str, source: str, match: re.Match[str]) -> GuardFinding:
    line = source.count("\n", 0, match.start()) + 1
    text = source.splitlines()[line - 1].strip()
    if rule == "raw-offset":
        text += (
            "; accepted form: pointer->field with the typed field at the measured offset"
            " in a shared paths.include header"
        )
    return GuardFinding(rule, line, text, None)


def _matches(rule: str, pattern: str, source: str, code: str) -> list[GuardFinding]:
    return [_finding(rule, source, m) for m in re.finditer(pattern, code, re.M)]


def _asm(source: str, code: str) -> list[GuardFinding]:
    pasted = re.sub(r"\s*##\s*", "", code)
    return _matches("inline-asm", r"\b(?:asm|__asm|__asm__|GLOBAL_ASM|INCLUDE_ASM)\b", source, pasted)


def _volatile(source: str, code: str) -> list[GuardFinding]:
    allowed = set()
    # A dereferenced pointer cast can force ordering without declaring storage volatile.
    for cast in re.finditer(r"\*\s*\([^();{}]*\bvolatile\b[^();{}]*\*\s*\)", code):
        allowed.add(code.index("volatile", cast.start(), cast.end()))
    return [
        _finding("volatile-storage", source, m) for m in re.finditer(r"\bvolatile\b", code) if m.start() not in allowed
    ]


def _offsets(source: str, code: str) -> list[GuardFinding]:
    findings = []
    # Follow balanced expressions after a unary dereference; nested casts are common.
    for match in re.finditer(r"\*\s*\(", code):
        start = code.index("(", match.start())
        depth = 0
        end = start
        for end in range(start, len(code)):
            if code[end] == "(":
                depth += 1
            elif code[end] == ")":
                depth -= 1
                if depth == 0:
                    break
            elif code[end] in ";{}":
                break
        expression = code[start : end + 1]
        # The cast may be outside the arithmetic parentheses: *(T*)((char*)p + N).
        if end + 1 < len(code) and re.match(r"\s*\(", code[end + 1 :]):
            tail = re.match(r"\s*\(([^;\n{}]*)", code[end + 1 :])
            expression += tail[0] if tail else ""
        if re.search(r"\([^()]*\*\s*\)", expression) and re.search(r"[+-]\s*(?:0[xX][\da-fA-F]+|\d+)\b", expression):
            findings.append(_finding("raw-offset", source, match))
    return findings


def _includes(source: str, code: str) -> list[GuardFinding]:
    findings = []
    for match in re.finditer(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', source, re.M):
        directive = source.index("#", match.start(), match.end())
        if code[directive] != "#":
            continue
        name = match[1]
        if (
            name.startswith(("/", "\\"))
            or ".." in Path(name).parts
            or re.search(r"(?:scratch|nonmatching|drafts|tiers)/", name)
        ):
            findings.append(_finding("local-include", source, match))
    return findings


def _comments(source: str, code: str) -> list[GuardFinding]:
    findings = []
    for match in _LEX.finditer(source):
        if match[0].startswith(("/*", "//")) and re.search(
            r"\b(?:unbake|N64DecompTools|decomp-permuter|m2c|objdiff)\b", match[0], re.I
        ):
            findings.append(_finding("tool-comment", source, match))
    return findings


def _versions(source: str, code: str) -> list[GuardFinding]:
    directives = list(re.finditer(r"^\s*#\s*(if|ifdef|ifndef|endif|else|elif)\b([^\n]*)", code, re.M))
    findings = []
    for index, match in enumerate(directives):
        if match[1] not in ("if", "ifdef", "ifndef") or not re.search(r"\bVERSION_\w+\b", match[2]):
            continue
        depth = 1
        for closing in directives[index + 1 :]:
            depth += 1 if closing[1] in ("if", "ifdef", "ifndef") else -1 if closing[1] == "endif" else 0
            if depth == 0:
                outside = code[: match.start()] + code[closing.end() :]
                outside = re.sub(r"^\s*#.*$", "", outside, flags=re.M).strip()
                if not outside:
                    findings.append(_finding("file-version-guard", source, match))
                break
    return findings


def _empty_loop(source: str, code: str) -> list[GuardFinding]:
    return _matches("empty-loop", r"\bdo\s*\{\s*\}\s*while\s*\(\s*0\s*\)", source, code)


RULES = [
    Rule("inline-asm", "code", _asm),
    Rule("volatile-storage", "code", _volatile),
    Rule("raw-offset", "code", _offsets),
    Rule("local-include", "directives", _includes),
    Rule("tool-comment", "comments", _comments),
    Rule("file-version-guard", "directives", _versions),
    Rule("empty-loop", "code", _empty_loop),
]


def run(source: str | Path) -> list[GuardFinding]:
    """Inspect source text or a file, retaining marked findings as accepted evidence."""
    if isinstance(source, Path):
        try:
            source = source.read_text()
        except (OSError, UnicodeError) as error:
            raise Held("checks", f"source {source}: {error}") from error
    if not isinstance(source, str):
        raise Held("checks", "source is missing or invalid")
    reasons = fakematches(source)
    code = _code(source)
    findings = [finding for rule in RULES for finding in rule.check(source, code)]
    reason = "; ".join(reasons) if reasons else None
    return [GuardFinding(f.rule, f.line, f.text, reason) for f in sorted(findings, key=lambda f: (f.line, f.rule))]


def message(finding: GuardFinding) -> str:
    return f"{finding.rule}:{finding.line}: {finding.text}"


def resolve(findings: list[Need], project: object, policy: object) -> list[Edit]:
    """Refuse unmarked findings before any build; marked evidence requires no edit."""
    if not isinstance(findings, list):
        raise Held("checks", "findings is missing or invalid")
    for finding in findings:
        if not isinstance(finding, GuardFinding):
            raise Held("checks", "GuardFinding is missing or invalid")
        if not finding.fakematch:
            raise Held("checks", f"{finding.rule}:{finding.line}: {finding.text}")
    return []


def derive(context: object) -> list[Need]:
    """Attach source findings to a trial before match resolution."""
    source = getattr(context, "source", None)
    if not isinstance(source, Path):
        raise Held("checks", "trial_context.source is missing or invalid")
    return list(run(source))


register_deriver(derive)
register_resolver(GuardFinding, 0, resolve)
