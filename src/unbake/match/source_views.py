"""Select active source lines while retaining declaration edit offsets."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake.layout.header_context import Headers
from unbake.layout.structs_parser import Parser
from unbake.match.common import held
from unbake.project.config import Policy, Project
from unbake.project_tools import atomic as atomic_files


def parsers(
    project: Project, policy: Policy, text: str, versions: tuple[str, ...], headers: Headers | None = None
) -> list[Parser]:
    """Ask cpp to select branches without expanding tokens or changing edit spans."""
    context = headers if headers is not None else Headers.read(project)

    def contextual(view: str) -> Parser:
        # Preserve declaration offsets in the source while resolving imported
        # by-value types and callback aliases. Locally defined tags get their
        # own aggregates; imported aggregates retain their measured layouts.
        parser, _ = context.parse(view)
        return parser

    if not re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M):
        return [contextual(text)]
    lines = text.splitlines(keepends=True)
    result = []
    # Versions selecting the same lines share one parse.
    parsed: dict[frozenset[int], Parser] = {}
    for version in versions:
        active = _version_lines(project, policy, text, version)
        if active is None:
            active = _preprocessed_lines(project, policy, text, version, context)
        key = frozenset(active)
        if key not in parsed:
            view = "".join(
                line if index in active else "".join("\n" if char == "\n" else " " for char in line)
                for index, line in enumerate(lines)
            )
            parsed[key] = contextual(view)
        result.append(parsed[key])
    return result


def typed_context(
    project: Project,
    policy: Policy,
    headers: Headers,
    version: str,
    *,
    source_context: bool = False,
    context_project: Project | None = None,
) -> str:
    """Reuse preprocessing for an effective header set and selected version."""
    from unbake.project.cache import remembered
    from unbake.typemap import declarations as typed_declarations

    selection = (
        project.root,
        project.include,
        tuple(headers.texts.items()),
        policy.cpp,
        policy.cppflags,
        project.compilers[project.default_compiler].cflags,
        project.version(version).macros,
        source_context,
    )

    def compute() -> str:
        with tempfile.TemporaryDirectory(prefix="match-types-") as temporary:
            local = context_project
            if local is None:
                roots = header_includes(project, headers, Path(temporary))
                local = replace(project, include=roots, overlay_roots=roots)
            if source_context:
                # Source context includes promoted components excluded from header-only evidence.
                source = Path(temporary) / "rewrite-context.c"
                atomic_files.text(source, "")
                return typed_declarations.headers(
                    local, policy, version, extra=source, contents=authored_contents(project, headers, local)
                )
            return typed_declarations.headers(
                local, policy, version, contents=authored_contents(project, headers, local)
            )

    cached: dict[tuple[Any, ...], str] = headers.__dict__.setdefault("_typed_contexts", {})
    if selection not in cached:
        cached[selection] = remembered("match.typed-context", selection, compute, keep=8)
        if len(cached) > 8:
            cached.pop(next(iter(cached)))
    return cached[selection]


@contextmanager
def shared_includes(project: Project, headers: Headers) -> Iterator[None]:
    """Own an incrementally materialized include snapshot for serial folding."""
    with tempfile.TemporaryDirectory(prefix="match-includes-") as temporary:
        headers.__dict__["_shared_includes"] = (project.root, Path(temporary), {})
        try:
            yield
        finally:
            del headers.__dict__["_shared_includes"]


def authored_contents(project: Project, headers: Headers, local: Project) -> dict[Path, str]:
    from unbake.typemap import storage

    return {
        staged / path.relative_to(root): text
        for root, staged in zip(project.include, local.include, strict=True)
        for path, text in headers.texts.items()
        if path.is_relative_to(root)
        and not (isinstance(getattr(project, "build", None), Path) and storage.generated(project, path))
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
    project: Project, policy: Policy, text: str, version: str, headers: Headers | None = None
) -> set[int]:
    lines = text.splitlines(keepends=True)
    with tempfile.TemporaryDirectory(prefix="match-view-") as temporary:
        source = Path(temporary) / "source.c"
        atomic_files.text(source, text)
        include = header_includes(project, headers, Path(temporary)) if headers is not None else ()
        command = [
            str(policy.cpp),
            *(f"-I{root}" for root in (*include, *project.include)),
            *(flag for flag in policy.cppflags if flag != "-P"),
            "-fdirectives-only",
            *(f"-D{macro}" for macro in project.version(version).macros),
            str(source),
        ]
        completed = subprocess.run(command, cwd=project.root, capture_output=True, text=True)
    if completed.returncode:
        held(f"VERSION {version}: declaration preprocessing: {completed.stderr.strip()}")
    active: set[int] = set()
    current, number = "", 1
    for line in completed.stdout.splitlines():
        marker = re.match(r'^#\s+(\d+)\s+"([^"]+)"', line)
        if marker:
            number, current = int(marker[1]), marker[2]
            continue
        if line.strip() and current == str(source) and 1 <= number <= len(lines):
            active.add(number - 1)
        number += 1
    return active


_DIRECTIVE = re.compile(r"^\s*#\s*(\w+)\s*(.*?)\s*$")


def _version_lines(project: Project, policy: Policy, text: str, version: str) -> set[int] | None:
    """Select conditional branches whose tests name only VERSION and command-line macros.

    Returns None whenever a header could influence a test; cpp then decides.
    """
    code = re.sub(r"/\*.*?\*/", lambda match: re.sub(r"[^\n]", " ", match[0]), text, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    if "\\\n" in code:
        return None
    macros: dict[str, int | None] = {macro: 1 for macro in project.version(version).macros}
    known = {macro for name in project.versions for macro in project.version(name).macros} | {"NON_MATCHING"}
    for flag in policy.cppflags:
        if flag.startswith("-D"):
            name, _, value = flag[2:].partition("=")
            known.add(name)
            macros[name] = int(value) if re.fullmatch(r"\d+", value) else 1
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
