"""Source rules, candidate admission scope, and project debt."""

from __future__ import annotations

import pickle
import re
from bisect import bisect_left
from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path

from unbake import config, effort, native, pool, recipes, store, types
from unbake import view as _view
from unbake.contracts import Finding, Snapshot, SourceView, UnitSpec, digest

_CODE = digest(Path(__file__).read_bytes())


def _numbered(text: str):
    """text offset -> 1-based line, without counting newlines from the start for every hit."""
    breaks = [m.start() for m in re.finditer("\n", text)]
    return lambda offset: bisect_left(breaks, offset) + 1
def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)

def _scopes(text: str) -> dict[str, str]:
    code, comments, directives = list(text), list(_blank(text)), list(text)
    tokens = r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\''
    for match in re.finditer(tokens, text):
        start, end = match.span()
        code[start:end] = _blank(match[0])
        if match[0].startswith(("/*", "//")):
            comments[start:end] = match[0]
            directives[start:end] = _blank(match[0])
    clean = "".join(code).splitlines(keepends=True)
    directive_lines = "".join(directives).splitlines(keepends=True)
    continuation = False
    for index, line in enumerate(directive_lines):
        is_directive = continuation or line.lstrip().startswith("#")
        continuation = is_directive and line.rstrip().endswith("\\")
        if is_directive:
            clean[index] = _blank(clean[index])
        else:
            directive_lines[index] = _blank(line)
    return {"code": "".join(clean), "directives": "".join(directive_lines),
            "comments": "".join(comments)}

def _active(source: SourceView, path: str) -> str:
    lines = _view.active_lines(source, path)
    return "\n".join(lines.get(n, "") for n in range(1, max(lines, default=0) + 1))

def _volatile(code: str, ranges: Sequence[dict]) -> set[int]:
    allowed = set()
    cast = r"\(\s*[\w\s]*\bvolatile\b[\w\s]*\*\s*\)"
    for match in re.finditer(cast, code):
        stop = code.find(";", match.end())
        expression = code[match.end():stop if stop >= 0 else None]
        numbers = [int(m[0], 16 if m[0].lower().startswith("0x") else 10)
                   for m in re.finditer(r"\b(?:0[xX][\da-fA-F]+|[0-9]+)(?=[uUlL]*\b)", expression)]
        combined = 0
        for number in numbers:
            combined |= number
        if any(r["start"] <= n <= r["end"] for n in [*numbers, combined] for r in ranges):
            allowed.update(match.start() + m.start() for m in re.finditer(r"\bvolatile\b", match[0]))
    line = _numbered(code)
    return {line(m.start()) for m in re.finditer(r"\bvolatile\b", code) if m.start() not in allowed}

def _declarations(code: str) -> dict[str, int]:
    names, start, depth, line = {}, 0, 0, _numbered(code)
    for token in re.finditer(r"[{};]", code):
        if token[0] == "{":
            depth += 1
        elif token[0] == "}":
            depth -= 1
            prefix = code[start:token.start()]
            function_body = re.search(r"\)\s*$", prefix.split("{", 1)[0])
            if depth == 0 and (function_body or not re.search(r"\b(?:typedef|struct|union|enum)\b", prefix)):
                start = token.end()
        elif depth == 0:
            declaration = code[start:token.start()]
            pattern = None
            if (re.search(r"\btypedef\b", declaration)
                    or (re.search(r"\bextern\b", declaration) and "(" not in declaration)):
                pattern = r"\b(\w+)\s*(?:\[[^]]*\]\s*)?$"
            elif "=" not in declaration and "{" not in declaration:
                pattern = r"\b(\w+)\s*\([^;]*\)\s*$"
            match = re.search(r"\(\s*\*\s*(\w+)\s*\)", declaration) if pattern else None
            match = match or (re.search(pattern, declaration) if pattern else None)
            if match:
                names[match[1]] = line(start + match.start(1))
            start = token.end()
    return names

def _shared(code: str, source: SourceView, path: str) -> set[int]:
    header_names = set()
    for header, _ in source.dependencies:
        if header != path and header.endswith(".h"):
            header_names.update(_declarations(_scopes(_active(source, header))["code"]))
    return {line for name, line in _declarations(code).items() if name in header_names}

_LOOP_HEAD = re.compile(r"\b(?:for|while)\s*\([^;{}]*(?:;[^;{}]*;[^{}]*)?\)\s*;")

def _closes_do_body(code: str, start: int) -> bool:
    """Whether the loop head at start is the tail of a do { ... } while (...); statement."""
    before = code[:start].rstrip()
    if not before.endswith("}"):
        return False
    depth = 0
    for index in range(len(before) - 1, -1, -1):
        depth += {"}": 1, "{": -1}.get(before[index], 0)
        if depth == 0:
            return re.search(r"\bdo\s*$", before[:index]) is not None
    return False

def _empty_loops(code: str) -> set[int]:
    line = _numbered(code)
    return {line(m.start()) for m in _LOOP_HEAD.finditer(code)
            if not (m[0].startswith("while") and _closes_do_body(code, m.start()))}

_PAIR = re.compile(r"\s*\)?\s*,\s*0x[0-9A-Fa-f]{8}\b")
def _raw_gfx(code: str) -> set[int]:
    opcodes = set(config.load_resource("rules.toml")["gfx"]["opcodes"])
    result, line = set(), _numbered(code)
    for match in re.finditer(r"\b0x[0-9A-Fa-f]{8}\b", code):
        if int(match[0], 16) >> 24 not in opcodes:
            continue
        prefix = code[max(code.rfind(";", 0, match.start()), code.rfind("\n", 0, match.start())) + 1:match.start()]
        if (re.search(r"\bw0\s*=\s*\(?\s*$", prefix)
                or re.search(r"\bg(?:s)?[DS]P\w*\s*\([^;]*$", prefix)
                or (re.search(r"\{\s*\(?\s*$", prefix)
                    and _PAIR.match(code, match.end()))):
            result.add(line(match.start()))
    return result

def evaluate(snapshot: Snapshot, path: str, text: str, view: SourceView | None,
             sdk_group: bool) -> tuple[Finding, ...]:
    with effort.stage("policy.evaluate"):
        rules = config.load_resource("rules.toml")
        scopes, lines, findings = _scopes(text), text.splitlines(), []
        for rule in rules["rule"]:
            if "regex" in rule:
                scoped = scopes[rule["scope"]]
                number = _numbered(scoped)
                hits = {number(m.start() + len(m[0]) - len(m[0].lstrip()))
                        for m in re.finditer(rule["regex"], scoped, re.M)}
            elif rule["check"] == "empty-loop":
                hits = _empty_loops(scopes["code"])
            elif rule["check"] == "volatile":
                active = scopes["code"] if sdk_group or not view else _scopes(_active(view, path))["code"]
                hits = set() if sdk_group else _volatile(active, rules["hardware"]["address_ranges"])
            elif rule["check"] == "shared-declarations":
                hits = _shared(scopes["code"], view, path) if view else set()
            else:
                hits = _raw_gfx(scopes["code"])
            comment_lines = scopes["comments"].splitlines()
            for n in sorted(hits):
                adjacent = "\n".join(comment_lines[max(0, n - 2):n])
                waivers = re.findall(r"/\*\s*FAKEMATCH:((?:[^*]|\*(?!/))*)\*/", adjacent)
                if rule["waivable"] and any(reason.strip() for reason in waivers):
                    continue
                line = lines[n - 1] if n <= len(lines) else ""
                findings.append(Finding(f"source.{rule['id']}",
                                        reason=f"{rule['sentence']}: {line.strip()[:120]}",
                                        path=path, line=n, blocking=True))
        return tuple(findings)

def scope(before: Sequence[Finding], after: Sequence[Finding], writes: Collection[str]
          ) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    prior = {(f.key, f.path, f.reason) for f in before}
    blocking, debt = [], []
    for finding in after:
        if finding.path in writes and (finding.key, finding.path, finding.reason) not in prior:
            blocking.append(replace(finding, blocking=True))
        else:
            debt.append(replace(finding, blocking=False))
    return tuple(blocking), tuple(debt)

def _census_version(snapshot: Snapshot, unit: UnitSpec) -> str:
    holders = sorted({p.version for name in unit.members for p in snapshot.layout.members[name].placements})
    return snapshot.config.project.names_from if snapshot.config.project.names_from in holders else holders[0]
def _census_unit(item: tuple[Snapshot, UnitSpec]) -> tuple[Finding, ...]:
    snapshot, unit = item
    source = _view.get(snapshot, unit, _census_version(snapshot, unit), recipes.resolve(snapshot.config, unit, {}))
    sdk = snapshot.layout.groups[unit.group].sdk
    text = snapshot.read(unit.path).decode()
    # evaluate reads the unit text and the active lines of the unit and its headers: other edits keep the verdict warm
    files = [unit.path, *(p for p, _ in source.dependencies if p.endswith(".h"))]
    key = digest((text, sdk, config.load_resource("rules.toml"), _CODE, [_active(source, p) for p in files]))
    return pickle.loads(store.cached(snapshot.config, "census", key,
                                     lambda: pickle.dumps(evaluate(snapshot, unit.path, text, source, sdk))))
def _census_warnings(item: tuple[Snapshot, UnitSpec]) -> tuple[Finding, ...]:
    """The unit's landed view conversions, debt by count: they are never a refusal."""
    snapshot, unit = item
    with store.work(snapshot.config) as work:
        found = native.warnings(snapshot, unit, _census_version(snapshot, unit),
                                recipes.resolve(snapshot.config, unit, {}), work)
    if not found:
        return ()
    return (Finding("land.view-conversion", f"{len(found)} integer-pointer conversions the compiler reports",
                    path=unit.path, missing=found, action="type each variable as the pointer it holds"),)
def _census_key(item: tuple[Snapshot, UnitSpec]) -> str | None:
    """Everything a build of the unit reads: warm while none of it changed, so the pool dispatches nothing."""
    snapshot, unit = item
    reads = _view.closure(snapshot, unit, _census_version(snapshot, unit))
    return None if reads is None else digest((reads, snapshot.layout.groups[unit.group].sdk,
                                              config.load_resource("rules.toml")))
def _census_header(item: tuple[Snapshot, str, bool]) -> tuple[Finding, ...]:
    snapshot, path, sdk = item  # a header is judged by its text alone, so one verdict serves every unit
    return evaluate(snapshot, path, snapshot.read(path).decode(), None, sdk)
def _header_key(item: tuple[Snapshot, str, bool]) -> str:
    snapshot, path, sdk = item
    return digest((snapshot.read(path), path, sdk, config.load_resource("rules.toml")))

def census(snapshot: Snapshot) -> tuple[Finding, ...]:
    """Every source once and every header once: a header that thousands of units include is read one time."""
    with effort.stage("policy.census"):
        kinds = config.load_resource("units.toml")["kind"]
        items = [(snapshot, unit) for unit in snapshot.layout.units.values()
                 if kinds[unit.kind]["credit"] == "code"
                 and "compile" in kinds[unit.kind]["phases"]]
        unique, headers = {}, {}
        for _, unit in items:  # a header is judged once, as SDK only when every unit that reads it is
            for path, _ in _view.closure(snapshot, unit, _census_version(snapshot, unit)) or ():
                if path.endswith(".h"):
                    headers[path] = headers.get(path, True) and snapshot.layout.groups[unit.group].sdk
        found = pool.gather(snapshot.config, [
            ("policy.census", _census_unit, items, _census_key),
            ("policy.census.headers", _census_header, [(snapshot, path, sdk) for path, sdk in sorted(headers.items())],
             _header_key),
            ("policy.census.warnings", _census_warnings, items, _census_key)])
        for findings in (*found[0], *found[1], *found[2], types.conflicts(snapshot)):
            for finding in findings:
                unique[(finding.key, finding.path, finding.line, finding.unit)] = replace(finding, blocking=False)
        return tuple(unique.values())
