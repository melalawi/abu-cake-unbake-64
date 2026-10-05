"""Source rules and match refusals with explicit, recorded exceptions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.cache import Cache, key
from unbake.config import Held, Host, Project
from unbake.decomp.gbi_source import invocations, macros, typedefs
from unbake.decomp.needs import GuardFinding, Need, register_resolver
from unbake.layout.split import Edit
from unbake.typemap import storage

SOURCE_FINDINGS_SCHEMA = 2


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
    if rule == "volatile-storage":
        text += (
            "; accepted form: qualified shared declaration validated against the symbol inventory,"
            " device access, or qualifier removal proved identical in every owning VERSION and mode"
        )
    return GuardFinding(rule, line, text, None)


def _matches(rule: str, pattern: str, source: str, code: str) -> list[GuardFinding]:
    return [_finding(rule, source, m) for m in re.finditer(pattern, code, re.M)]


def _asm(source: str, code: str) -> list[GuardFinding]:
    pasted = re.sub(r"\s*##\s*", "", code)
    return _matches("inline-asm", r"\b(?:asm|__asm|__asm__|GLOBAL_ASM|INCLUDE_ASM)\b", source, pasted)


class _Syntax:
    """Balanced C tokens shared by expression-based guards."""

    def __init__(self, code: str) -> None:
        self.tokens = list(
            re.finditer(r"[A-Za-z_]\w*|0[xX][\da-fA-F]+[uUlL]*|\d+(?:\.\d*)?[\w]*|->|\+\+|--|[^\s]", code)
        )
        self.words = [token[0] for token in self.tokens]
        self.pairs: dict[int, int] = {}
        stack: list[int] = []
        for index, word in enumerate(self.words):
            if word in ("(", "[", "{"):
                stack.append(index)
            elif word in (")", "]", "}") and stack:
                opening = stack.pop()
                if self.words[opening] == {")": "(", "]": "[", "}": "{"}[word]:
                    self.pairs[opening] = index

    def pointer_type(self, start: int, end: int) -> bool:
        words = self.words[start + 1 : end]
        return "*" in words and all(re.fullmatch(r"[A-Za-z_]\w*|\d+|[*(),\[\]]", word) for word in words)

    def unary(self, index: int) -> bool:
        if not index:
            return True
        previous = self.words[index - 1]
        if previous == ")":
            opening = next((a for a, b in self.pairs.items() if b == index - 1), -1)
            if opening >= 0 and re.fullmatch(
                r"(?:[su](?:8|16|32|64)|f(?:32|64)|int|char|short|long|float|double)",
                " ".join(self.words[opening + 1 : index - 1]),
            ):
                return True
        return previous in ("return", "case", "sizeof") or not re.fullmatch(r"[\w]+|[)\]]|\+\+|--", previous)

    def operand_end(self, start: int) -> int:
        """Bound one unary operand, including casts and postfix expressions."""
        if start >= len(self.words):
            return start
        word = self.words[start]
        if word in ("*", "&", "+", "-", "!", "~", "++", "--"):
            return self.operand_end(start + 1)
        if word == "(" and start in self.pairs:
            closing = self.pairs[start]
            if self.pointer_type(start, closing):
                return self.operand_end(closing + 1)
            end = closing + 1
        else:
            end = start + 1
        while end < len(self.words):
            if self.words[end] in ("(", "[") and end in self.pairs:
                end = self.pairs[end] + 1
            elif self.words[end] in (".", "->"):
                end += 2
            else:
                break
        return end

    def scope(self, index: int) -> tuple[int, int]:
        scopes = [(a, b) for a, b in self.pairs.items() if self.words[a] == "{" and a < index < b]
        return max(scopes, default=(0, len(self.words)))


def volatile_tokens(code: str) -> list[re.Match[str]]:
    """Return refused qualifier tokens with their original source offsets."""
    syntax = _Syntax(code)
    words = syntax.words
    allowed: set[int] = set()
    # Type queries do not declare storage. Parentheses around a dereferenced cast
    # likewise do not change the existing cast exception.
    for opening, closing in syntax.pairs.items():
        if words[opening] != "(":
            continue
        if opening and words[opening - 1] in ("sizeof", "_Alignof", "alignof"):
            allowed.update(range(opening, closing))
        elif syntax.pointer_type(opening, closing):
            preceding = opening - 1
            while preceding >= 0 and words[preceding] == "(":
                preceding -= 1
            if preceding >= 0 and words[preceding] == "*" and syntax.unary(preceding):
                allowed.update(range(opening, closing))

    # A pointer to a literal device address, and aliases of that pointer, qualify
    # the pointed-to device rather than introducing volatile local storage.
    devices: list[tuple[str, int, int]] = []
    declarations = []
    for index, word in enumerate(words):
        if word != "*" or not index or not re.fullmatch(r"[A-Za-z_]\w*|\*", words[index - 1]):
            continue
        name = index + 1
        while name < len(words) and words[name] in ("volatile", "const", "restrict"):
            name += 1
        if (
            name + 1 < len(words)
            and re.fullmatch(r"[A-Za-z_]\w*", words[name])
            and words[name + 1] in (";", "=", ",", "[")
        ):
            declarations.append((words[name], name, *syntax.scope(name)))

    def binding(name: str, index: int) -> tuple[str, int, int]:
        scopes = [(lo, hi) for value, at, lo, hi in declarations if value == name and at <= index and lo < index < hi]
        lo, hi = max(scopes, default=syntax.scope(index))
        return name, lo, hi

    declarators = {at for _, at, _, _ in declarations}
    assignments = [
        i
        for i, word in enumerate(words)
        if word == "="
        and i
        and re.fullmatch(r"[A-Za-z_]\w*", words[i - 1])
        and (i < 2 or words[i - 2] not in (".", "->"))
        and (i < 2 or words[i - 2] != "*" or i - 1 in declarators)
    ]
    literals = set()
    for index in assignments:
        start = index + 1
        opening = start
        while opening < len(words) and words[opening] == "(" and opening in syntax.pairs:
            closing = syntax.pairs[opening]
            if syntax.pointer_type(opening, closing):
                value = closing + 1
                while value < len(words) and words[value] == "(":
                    value += 1
                if value < len(words) and re.fullmatch(r"0[xX][\da-fA-F]+[uUlL]*", words[value]):
                    address = int(words[value].rstrip("uUlL"), 16)
                    if 0xA4000000 <= address < 0xA8000000:
                        allowed.update(range(opening, closing))
                        literals.add(index)
                        devices.append(binding(words[index - 1], index))
                break
            opening += 1
    for _ in assignments:
        changed = False
        for index in assignments:
            value = index + 1
            if value + 1 < len(words) and words[value + 1] == ";" and binding(words[value], index) in devices:
                alias = binding(words[index - 1], index)
                if alias not in devices:
                    devices.append(alias)
                    changed = True
        if not changed:
            break
    # One unrelated assignment invalidates a device alias and its dependants.
    # A device assignment in a nested block refers to the declared pointer's
    # scope, while a shadow declaration remains a different binding.
    for _ in assignments:
        invalid: set[tuple[str, int, int]] = set()
        for index in assignments:
            target = binding(words[index - 1], index)
            value = index + 1
            is_alias = value + 1 < len(words) and words[value + 1] == ";" and binding(words[value], index) in devices
            if target in devices and index not in literals and not is_alias:
                invalid.add(target)
        if not invalid:
            break
        devices = [device for device in devices if device not in invalid]
    for index, word in enumerate(words):
        if word != "volatile":
            continue
        tail = re.match(r"\s*(?:[A-Za-z_]\w*\s+)*\*\s*([A-Za-z_]\w*)", code[syntax.tokens[index].end() :])
        if tail and binding(tail[1], index + 1) in devices:
            allowed.add(index)
        # A function return qualifier is not a storage declaration.
        if re.match(r"\s*(?:[A-Za-z_]\w*\s+)+[A-Za-z_]\w*\s*\(", code[syntax.tokens[index].end() :]):
            allowed.add(index)
    return [token for index, token in enumerate(syntax.tokens) if token[0] == "volatile" and index not in allowed]


def _volatile(source: str, code: str) -> list[GuardFinding]:
    return [_finding("volatile-storage", source, token) for token in volatile_tokens(code)]


def _direct_offsets(source: str, code: str) -> list[GuardFinding]:
    syntax = _Syntax(code)
    findings = []
    for index, token in enumerate(syntax.tokens):
        if (
            token[0] != "*"
            or index + 1 >= len(syntax.words)
            or syntax.words[index + 1] != "("
            or not syntax.unary(index)
        ):
            continue
        end = syntax.operand_end(index + 1)
        expression = code[token.end() : syntax.tokens[end - 1].end()] if end > index + 1 else ""
        # Offsets inside a nested loaded value belong to that load alone.
        for nested in range(index + 1, end):
            if syntax.words[nested] == "*" and syntax.unary(nested) and syntax.words[nested + 1] == "(":
                nested_end = min(syntax.operand_end(nested + 1), end)
                lo = syntax.tokens[nested].start() - token.end()
                hi = syntax.tokens[nested_end - 1].end() - token.end()
                expression = expression[:lo] + " " * (hi - lo) + expression[hi:]
        has_cast = any(
            index < opening < closing < end and syntax.pointer_type(opening, closing)
            for opening, closing in syntax.pairs.items()
        )
        offsets = re.finditer(r"[+-]\s*\(*\s*(0[xX][\da-fA-F]+|\d+)\b", expression)
        if has_cast and any(int(offset[1], 16 if offset[1].lower().startswith("0x") else 10) for offset in offsets):
            findings.append(_finding("raw-offset", source, token))
    return findings


def _offsets(source: str, code: str) -> list[GuardFinding]:
    definitions = macros(code)
    executable = re.sub(r"^[ \t]*#(?:[^\n]*\\\n)*[^\n]*", lambda m: " " * len(m[0]), code, flags=re.M)
    findings = _direct_offsets(source, executable)

    def expand_fields(text: str, depth: int = 0) -> str:
        if depth >= 12:
            return text
        for name, macro in definitions.items():
            for start, end, args in reversed(invocations(text, name)):
                if len(args) != len(macro.parameters) or "#" in macro.body:
                    continue
                replacements = dict(zip(macro.parameters, args, strict=True))

                def replace_token(match: re.Match[str], values: dict[str, str] = replacements) -> str:
                    return values.get(match[0], match[0])

                body = re.sub(r"\b\w+\b", replace_token, macro.body)
                text = text[:start] + expand_fields(body, depth + 1) + text[end:]
        return text

    for name in definitions:
        for start, end, _ in invocations(executable, name):
            expanded = expand_fields(code[start:end])
            if _direct_offsets(expanded, expanded):
                line = source.count("\n", 0, start) + 1
                text = source.splitlines()[line - 1].strip()
                findings.append(
                    GuardFinding("raw-offset", line, text + "; local macro expands to numeric field access", None)
                )
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


def _resident(source: str, code: str) -> list[GuardFinding]:
    """Resident constant storage restates ROM bytes the link discards; slices supply them."""
    return _matches("resident-storage", r"\bunbake_rodata_\w+", source, code)


def _gfx(source: str, code: str) -> list[GuardFinding]:
    findings = _matches("raw-gfx", r"(?:\.|->)\s*(?:words\s*[._]\s*)?w[01]\s*(?:[|&^+\-]?=(?!=)|\+\+|--)", source, code)
    # Hidden packets still require SDK exposure. Recognize the command tag in
    # the first store, and packet-stride copies guarded by a command sentinel.
    from unbake.decomp.gbi import OP_NAMES
    from unbake.decomp.gbi_expr import Ambiguous, Word, integer, scalar, split
    from unbake.decomp.gbi_source import packet_pointers

    opcodes = set(OP_NAMES) | {0x01, 0x05, 0x06, 0x08, 0xAF, 0xB6, 0xB7, 0xB8, 0xBF, 0xDD}
    pairs = re.finditer(
        r"(?P<ptr>\b\w+)\s*->\s*(?P<first>\w+)\s*=(?!=)\s*(?P<value>[^;]+);\s*"
        r"(?P=ptr)\s*->\s*(?P<second>\w+)\s*=(?!=)\s*[^;]+;",
        code,
    )
    packet_sentinel = any(int(m[0], 16) >> 24 in opcodes for m in re.finditer(r"0[xX][0-9A-Fa-f]{8}\b", code))
    gfx_pointers = packet_pointers(code, "Gfx")
    for match in pairs:
        if match["first"] == match["second"]:
            continue
        try:
            # Word.parse normalizes constants modulo 2**32 for decoding. A
            # signed scalar sentinel (-1, -2, ~0) is not an opcode word merely
            # because sign extension happens to put a command tag in byte 3.
            terms = split(scalar(match["value"]), "|")
            constants = [integer(term) for term in terms]
            opcode = (
                Word.parse(match["value"]).fixed(24, 8)
                if all(value is None or 0 <= value <= 0xFFFFFFFF for value in constants)
                else None
            )
        except Ambiguous:
            opcode = None
        stride_copy = (
            match["first"] == "unk0"
            and match["second"] == "unk4"
            and packet_sentinel
            and re.search(r"->\s*unk8\b", code)
            and integer(match["value"]) is None
        )
        if match["ptr"] in gfx_pointers or opcode in opcodes or stride_copy:
            findings.append(_finding("raw-gfx", source, match))
    lines = source.splitlines()
    # Only the lowering boundary emits this per-line diagnostic for an
    # independently unrepresentable packet; ordinary raw writes stay refused.
    return [
        finding for finding in findings if not re.search(r"/\*\s*GBI_RAW:\s*[^*\s][^*]*\*/", lines[finding.line - 1])
    ]


def _copies(source: str, code: str) -> list[GuardFinding]:
    copies = []
    for name, (start, _, _) in typedefs(code).items():
        if re.fullmatch(r"[su](?:8|16|32|64)|f(?:32|64)|Gfx|M2C_UNK(?:8|16|32|64)?", name):
            line = source.count("\n", 0, start) + 1
            copies.append(GuardFinding("local-type-copy", line, source.splitlines()[line - 1].strip(), None))
    return (
        copies
        + _matches("local-gbi-macro", r"^\s*#\s*define\s+(?:_SHIFTL|_SHIFTR|g(?:s)?[DS]P\w+)\b", source, code)
        + _matches("invented-struct", r"\bstruct\s+(?:func_[A-Fa-f0-9]+_S\w*|Layout_\w+)\s*\{", source, code)
        + _matches("symbol-alias", r"^\s*#\s*define\s+\w+\s+[^\n]*\b0[xX]8[0-9a-fA-F]{7}\b", source, code)
    )


RULES = [
    Rule("inline-asm", "code", _asm),
    Rule("volatile-storage", "code", _volatile),
    Rule("raw-offset", "code", _offsets),
    Rule("local-include", "directives", _includes),
    Rule("tool-comment", "comments", _comments),
    Rule("file-version-guard", "directives", _versions),
    Rule("empty-loop", "code", _empty_loop),
    Rule("resident-storage", "code", _resident),
    Rule("raw-gfx", "code", _gfx),
    Rule("shared-declarations", "code", _copies),
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
    return [
        GuardFinding(
            f.rule,
            f.line,
            f.text,
            None
            if f.rule in {"raw-gfx", "local-gbi-macro", "local-type-copy", "invented-struct", "resident-storage"}
            else reason,
        )
        for f in sorted(findings, key=lambda f: (f.line, f.rule))
    ]


def unmarked(source: str | Path) -> list[GuardFinding]:
    """Findings without a FAKEMATCH reason: what refuses a land and holds `unbake check`."""
    return [finding for finding in run(source) if finding.fakematch is None]


def _has_unmarked(source: Path) -> bool:
    """Pool worker: SOURCE has a finding without a FAKEMATCH reason."""
    return bool(unmarked(source))


def dirty(project: Project, cache: Cache, sources: list[Path], host: Host) -> list[Path]:
    """The sources with an unmarked finding, cached on every source's stat signature (a change to any source
    rescans them all once, in the worker pool). The cache holds project-relative paths."""
    from unbake import inputs, pool

    named = [storage.relative(project, path) for path in sources]
    content_key = key(
        str(SOURCE_FINDINGS_SCHEMA),
        *(f"{name}\0{inputs.signature(path)}" for name, path in zip(named, sources, strict=True)),
    )

    def make(path: Path) -> None:
        found = pool.run(host, _has_unmarked, sources)
        atomic_files.fresh(path, json.dumps([name for name, bad in zip(named, found, strict=True) if bad]).encode())

    return [project.root / name for name in json.loads(cache.produce("source-findings", content_key, make).read_text())]


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


register_resolver(GuardFinding, 0, resolve)
