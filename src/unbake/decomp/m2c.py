"""Generate one C draft using the configured m2c and project header context."""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path

from unbake.decomp import similar
from unbake.decomp.commands import prefix
from unbake.decomp.draft_context import ordered_headers, required_headers
from unbake.decomp.draft_input import (
    assembly_source,
    canonical_entry,
    header_types,
    jump_tables,
    version_for,
    whole_body,
)
from unbake.decomp.field_access import share
from unbake.decomp.trial_compile import executable, read_text, run_tool, scratch_directory
from unbake.layout.structs import preprocess
from unbake.project.config import Held, Policy, Project


def _headers(project: Project) -> list[tuple[Path, str]]:
    if not project.include:
        raise Held("m2c", "paths.include is missing or empty")
    headers = []
    seen = set()
    for directory in project.include:
        directory = Path(directory)
        if not directory.is_dir():
            raise Held("m2c", f"paths.include directory {directory} is missing")
        for path in sorted(directory.rglob("*.h")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            headers.append((resolved, path.relative_to(directory).as_posix()))
    if not headers:
        raise Held("m2c", f"paths.include {project.include}: project headers are missing")
    return headers


def _context(headers: list[tuple[Path, str]], selected: set[Path]) -> str:
    paths = {path for path, _ in headers}
    contents = {path: read_text(path, "m2c") for path, _ in headers}
    relative_paths: dict[str, Path] = {}
    for path, relative in headers:
        if relative in relative_paths and relative_paths[relative] != path:
            raise Held("m2c", f"paths.include has ambiguous header {relative}")
        relative_paths[relative] = path
    expanded = set()

    def expand(path: Path) -> str:
        if path in expanded:
            return ""
        expanded.add(path)
        lines = [f"/* {path} */"]
        for line in contents[path].splitlines():
            include = re.match(r'^\s*#\s*include\s*["<]([^">]+)[">]', line)
            if include:
                local = (path.parent / include[1]).resolve()
                target = local if local in paths else relative_paths.get(include[1])
                if target is None:
                    raise Held("m2c", f"{path}: context header {include[1]} is missing from paths.include")
                lines.append(expand(target))
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    return "\n".join(expand(path) for path in ordered_headers(contents) if path in selected)


def draft(project: Project, policy: Policy, function: str | None, v: str | None, scratch: Path) -> Path:
    if not isinstance(function, str) or not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held("m2c", "function is required and must be a C identifier")
    v = version_for(project, v, "m2c")
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v):
        raise Held("m2c", f"version {v!r} cannot name a scratch directory")
    project.version(v)
    directory = scratch_directory(project, scratch, "m2c")
    assembly_path, address = assembly_source(project, v, function)
    executable_path = executable(getattr(policy, "m2c", None), "m2c", "m2c")
    targets = {"sn64": "mips-gcc-c", "ido": "mips-ido-c"}
    compiler = project.compiler_for(project.src / (function + ".c"))
    if compiler.kind not in targets:
        raise Held(
            "m2c", f"compiler.kind {project.compiler_for(project.src / (function + '.c')).kind!r} has no m2c target"
        )
    headers = _headers(project)
    work = Path(tempfile.mkdtemp(prefix=function + ".m2c.", dir=directory))
    context = work / "context.c"
    examples = similar.retrieve(project, function, v)
    examples_context = similar.context(examples)
    (work / "similar-context.txt").write_text(examples_context, encoding="utf-8")
    # Headers are expanded once above; includes in landed units would repeat
    # them. Preserve their definitions and macros for the context preprocessor.
    landed = "\n".join(re.sub(r"^\s*#\s*include[^\n]*", "", item.c, flags=re.M) for item in examples)
    context.write_text(_context(headers, {path for path, _ in headers}) + "\n" + landed, encoding="utf-8")
    context.write_text(preprocess(context, project, policy, v) + "\n" + examples_context, encoding="utf-8")
    print(
        "similar context used: "
        + (
            ", ".join(
                f"{item.function} (distance={item.distance:.6f}, edits={item.edit_distance})" for item in examples
            )
            or "none"
        )
    )
    assembly = work / (function + ".s")
    body = whole_body(canonical_entry(project, v, function, address, read_text(assembly_path, "m2c")), function)
    assembly.write_text(jump_tables(project, v, function, body), encoding="utf-8")
    output = run_tool(
        [
            executable_path,
            "-t",
            targets[compiler.kind],
            "--valid-syntax",
            "--context",
            str(context),
            "--function",
            function,
            str(assembly),
        ],
        work,
        "m2c",
    )
    if not output.strip():
        raise Held("m2c", f"policy.m2c {executable_path} produced no draft for {function}")
    source = work / (function + ".c")
    output = header_types(output, context.read_text())
    source.write_text(output, encoding="utf-8")
    print(f"draft_path: {source}")
    print(f"source filename: {function}.c (decomp try identifies the function from the filename)")
    selected = required_headers({path: read_text(path, "m2c") for path, _ in headers}, output)
    context.write_text(_context(headers, selected), encoding="utf-8")
    context.write_text(preprocess(context, project, policy, v), encoding="utf-8")
    output, shared = share(project, function, output, context.read_text())
    if shared is not None and shared.resolve() not in {path for path, _ in headers}:
        headers.append((shared.resolve(), shared.relative_to(project.include[0]).as_posix()))
    if shared is not None:
        selected.add(shared.resolve())
    # A single expansion also handles unguarded dependency headers included by
    # multiple roots. Keep the draft standalone without repeating their types.
    context.write_text(_context(headers, selected), encoding="utf-8")
    declarations = preprocess(context, project, policy, v)
    source.write_text(
        f"/* NON_MATCHING: draft of {function}; verify behavior and bytes before match. */\n"
        f"{declarations.rstrip()}\n\n{output.rstrip()}\n",
        encoding="utf-8",
    )
    unresolved = sorted(set(re.findall(r"\bM2C_\w+", output)))
    if unresolved:
        command = shlex.join([*prefix(project), "decomp", "guide", function, "--version", v])
        raise Held("m2c", f"draft {source}: missing declarations for {', '.join(unresolved)}; resolve: {command}")
    return source
