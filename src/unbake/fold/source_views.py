"""Select active source lines while retaining declaration edit offsets."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import scratch
from unbake.cdecl import LayoutParser
from unbake.config import Host, Project
from unbake.layout.header_context import Headers


def parsers(
    project: Project, policy: Host, text: str, versions: tuple[str, ...], unit: str, headers: Headers | None = None
) -> list[LayoutParser]:
    """Ask cpp to select branches without expanding tokens or changing edit spans."""
    context = headers if headers is not None else Headers.read(project)

    def contextual(view: str) -> LayoutParser:
        # Preserve declaration offsets in the source while resolving imported
        # by-value types and callback aliases. Locally defined tags get their
        # own aggregates; imported aggregates retain their measured layouts.
        parser, _ = context.parse(view)
        return parser

    if not re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M):
        return [contextual(text)]
    result = []
    parsed: dict[str, LayoutParser] = {}
    for version in versions:
        view = active_source(project, policy, text, version, unit, context)
        if view not in parsed:
            parsed[view] = contextual(view)
        result.append(parsed[view])
    return result


def active_source(
    project: Project, policy: Host, text: str, version: str, unit: str, headers: Headers | None = None
) -> str:
    """Select source-owned lines while keeping original tokens and edit offsets."""
    active = _version_lines(project, policy, text, version, unit)
    if active is None:
        active = _preprocessed_lines(project, policy, text, version, unit, headers)
    return "".join(
        line if index in active else "".join("\n" if char == "\n" else " " for char in line)
        for index, line in enumerate(text.splitlines(keepends=True))
    )


def typed_context(
    project: Project,
    policy: Host,
    headers: Headers,
    version: str,
    unit: str,
    *,
    context_project: Project | None = None,
) -> str:
    """Reuse preprocessing for an effective header set and selected version."""
    from unbake.cache import memo
    from unbake.compilers import drivers
    from unbake.typemap import declarations as typed_declarations

    selection = (
        project.root,
        project.include,
        tuple(headers.texts.items()),
        policy.cpp,
        project.cppflags,
        project.compiler_for(unit).id,
        project.compiler_for(unit).cc,
        tuple(drivers.flags(project, version, unit)),
        project.version(version).macros,
    )

    def compute() -> str:
        with scratch.temporary(policy, project, "fold", prefix="match-types-") as temporary:
            local = context_project
            if local is None:
                roots = header_includes(project, headers, Path(temporary))
                local = replace(project, work_include=tuple(roots))
            source = Path(temporary) / f"{unit}.c"
            atomic_files.text(source, "")
            return typed_declarations.headers(
                local, policy, version, extra=source, contents=authored_contents(project, headers, local)
            )

    cached: dict[tuple[Any, ...], str] = headers.__dict__.setdefault("_typed_contexts", {})
    if selection not in cached:
        cached[selection] = memo("match.typed-context", selection, compute, keep=8)
        if len(cached) > 8:
            cached.pop(next(iter(cached)))
    return cached[selection]


@contextmanager
def shared_includes(project: Project, headers: Headers, policy: Host) -> Iterator[None]:
    """Own an incrementally materialized include snapshot for serial folding."""
    with scratch.temporary(policy, project, "fold", prefix="match-includes-") as temporary:
        headers.__dict__["_shared_includes"] = (project.root, Path(temporary), {})
        try:
            yield
        finally:
            del headers.__dict__["_shared_includes"]


def authored_contents(project: Project, headers: Headers, local: Project) -> dict[Path, str]:
    from unbake.typemap import storage

    generated = (
        storage.generated_view(project) if isinstance(getattr(project, "build", None), Path) else lambda path: False
    )
    return {
        staged / path.relative_to(root): text
        for root, staged in zip(project.include, local.work_include, strict=True)
        for path, text in headers.texts.items()
        if path.is_relative_to(root) and not generated(path)
    }


def header_includes(project: Project, headers: Headers, directory: Path) -> tuple[Path, ...]:
    """Materialize the fold's effective include tree for every declaration preprocessor."""
    from unbake.layout import index as ownership_index

    texts = dict(headers.texts)
    for path in ownership_index.headers(project) if isinstance(getattr(project, "build", None), Path) else ():
        if path not in texts and path.is_file():
            texts[path] = path.read_text()
    shared = headers.__dict__.get("_shared_includes")
    before = {}
    if shared is not None and shared[0] == project.root:
        _, directory, before = shared
    roots = []
    for index, root in enumerate(project.include):
        staged = directory / "include" / str(index)
        staged.mkdir(parents=True, exist_ok=True)
        for path, content in texts.items():
            if path.is_relative_to(root) and before.get(path) != content:
                destination = staged / path.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.text(destination, content)
        # Links beside a private header copy (fold.apply.link_relative_includes) resolve its relative includes.
        for link in root.rglob("*") if root in getattr(project, "work_include", ()) else ():
            destination = staged / link.relative_to(root)
            if link.is_symlink() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(link.resolve())
        roots.append(staged)
    if shared is not None and shared[0] == project.root:
        for path in before.keys() - texts.keys():
            for root, staged in zip(project.include, roots, strict=True):
                if path.is_relative_to(root):
                    (staged / path.relative_to(root)).unlink(missing_ok=True)
        before.clear()
        before.update(texts)
    return tuple(roots)


def _preprocessed_lines(
    project: Project, policy: Host, text: str, version: str, unit: str, headers: Headers | None = None
) -> set[int]:
    lines = text.splitlines(keepends=True)
    with scratch.temporary(policy, project, "fold", prefix="match-view-") as temporary:
        source = Path(temporary) / "source.c"
        atomic_files.text(source, text)
        include = header_includes(project, headers, Path(temporary)) if headers is not None else ()
        # A header the tree does not have yet (the headers step regenerates it) is empty here, searched last:
        # only VERSION and command-line macros choose the source's lines.
        absent = Path(temporary) / "absent"
        for name in re.findall(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', text, re.M):
            if not any((root / name).is_file() for root in (*include, *project.include, absent)):
                (absent / name).parent.mkdir(parents=True, exist_ok=True)
                atomic_files.fresh(absent / name, b"")
        from unbake.compilers import drivers
        from unbake.process import run_tool

        local = replace(project, work_include=(*include, *project.work_include))
        command = drivers.preprocess_command(
            local, str(policy.cpp), version, unit, source, non_matching=False, line_markers=True
        )
        command[1:1] = [f"-I{project.src}"]
        command[-1:-1] = [f"-I{absent}"]
        output = run_tool(
            command, project.root, "solve", temporary_root=project.build, context={"function": unit, "version": version}
        )
    active: set[int] = set()
    current, number = "", 1
    for line in output.splitlines():
        marker = re.match(r'^#\s+(\d+)\s+"([^"]+)"', line)
        if marker:
            number, current = int(marker[1]), marker[2]
            continue
        if line.strip() and current == str(source) and 1 <= number <= len(lines):
            active.add(number - 1)
        number += 1
    return active


_DIRECTIVE = re.compile(r"^\s*#\s*(\w+)\s*(.*?)\s*$")


def _version_lines(project: Project, policy: Host, text: str, version: str, unit: str) -> set[int] | None:
    """Select conditional branches whose tests name only VERSION and command-line macros.

    Returns None whenever a header could influence a test; cpp then decides.
    """
    code = re.sub(r"/\*.*?\*/", lambda match: re.sub(r"[^\n]", " ", match[0]), text, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    if "\\\n" in code or re.search(r"^\s*#\s*include\b", code, re.M):
        return None
    from unbake.compilers import drivers

    compiler = project.compiler_for(unit)
    from unbake.compilers.families import family_for

    values = [*family_for(compiler).analysis_cppflags(project.cppflags), *drivers.flags(project, version, unit)]
    preprocess, _ = drivers._options(values)
    if "-include" in preprocess or "-imacros" in preprocess:
        return None
    macros: dict[str, int | None] = {}
    known = {macro.partition("=")[0] for name in project.versions for macro in project.version(name).macros} | {
        "NON_MATCHING"
    }
    options = iter(preprocess)
    for flag in options:
        value = next(options) if flag in drivers.PREPROCESSOR_PAIRS else flag[2:]
        if flag.startswith(("-D", "-U")):
            name, separator, spelling = value.partition("=")
            known.add(name)
            if flag.startswith("-U"):
                macros.pop(name, None)
            else:
                macros[name] = int(spelling) if re.fullmatch(r"\d+", spelling) else (None if separator else 1)
    stack: list[tuple[bool, bool]] = []  # (taking this branch, some branch already taken)
    active: set[int] = set()

    def test(expression: str) -> bool | None:
        def defined(match: re.Match[str]) -> str:
            name = match[1] or match[2]
            if name not in known:
                raise LookupError(name)
            return "1" if name in macros else "0"

        try:
            expression = re.sub(r"\bdefined\s*(?:\(\s*(\w+)\s*\)|(\w+))", defined, expression)
        except LookupError:
            return None
        for name in set(re.findall(r"\b[A-Za-z_]\w*\b", expression)):
            value = macros.get(name)
            if name not in known or value is None:
                return None
            expression = re.sub(rf"\b{name}\b", str(value), expression)
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression)
        if not re.fullmatch(r"[\d\s()<>=!andortn]*", expression):
            return None
        try:
            return bool(eval(expression, {"__builtins__": {}}))
        except SyntaxError:
            return None

    for index, line in enumerate(code.splitlines()):
        taking = all(entry[0] for entry in stack)
        directive = _DIRECTIVE.match(line)
        if directive is None:
            if taking:
                active.add(index)
            continue
        word, rest = directive[1], directive[2]
        if word in ("if", "ifdef", "ifndef"):
            outcome = test(rest if word == "if" else f"{'!' if word == 'ifndef' else ''}defined({rest})")
            if outcome is None:
                return None
            stack.append((outcome, outcome))
        elif word == "elif":
            if not stack:
                return None
            outcome = test(rest)
            if outcome is None:
                return None
            taken = stack[-1][1]
            stack[-1] = (outcome and not taken, taken or outcome)
        elif word == "else":
            if not stack:
                return None
            stack[-1] = (not stack[-1][1], True)
        elif word == "endif":
            if not stack:
                return None
            stack.pop()
        elif word in ("define", "undef") and taking:
            words = rest.split("(", 1)[0].split()
            name = words[0] if words else ""
            if not name:
                return None
            known.add(name)
            if word == "undef":
                macros.pop(name, None)
            else:
                value = rest[len(name) :].strip()
                macros[name] = int(value) if re.fullmatch(r"\d+", value) else (1 if not value else None)
    return active if not stack else None
