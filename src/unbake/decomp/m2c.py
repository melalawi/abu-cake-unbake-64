"""Generate one C draft using the configured m2c and project header context."""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path

from unbake.decomp import draft_abi, gbi, measured_storage, similar
from unbake.decomp import work as draft_work
from unbake.decomp.draft_asm import delay_slots, local_targets, saved_returns
from unbake.decomp.draft_compile import prove
from unbake.decomp.draft_context import ordered_headers, preprocess_context, required_headers
from unbake.decomp.draft_fp import command, register_pairs
from unbake.decomp.draft_input import (
    assembly_source,
    canonical_aliases,
    canonical_entry,
    header_types,
    jump_tables,
    private_constants,
    stack_locals,
    version_for,
    whole_body,
)
from unbake.decomp.draft_layouts import normalize
from unbake.decomp.draft_macros import lower
from unbake.decomp.draft_syntax import address_arithmetic
from unbake.decomp.field_access import share
from unbake.decomp.trial_compile import executable, read_text, run_tool, scratch_directory
from unbake.project.config import Held, Policy, Project
from unbake.project.headers import include_headers
from unbake.project_tools import atomic as atomic_files


def _headers(project: Project) -> list[tuple[Path, str]]:
    if not project.include:
        raise Held("m2c", "paths.include is missing or empty")
    for directory in project.include:
        if not Path(directory).is_dir():
            raise Held("m2c", f"paths.include directory {directory} is missing")
    headers = include_headers(project)
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
    # Include root headers in dependency order without copying their bodies.
    # Only unconditional includes can cover another selected root.
    graph: dict[Path, set[Path]] = {}
    for path, text in contents.items():
        graph[path] = set()
        clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", clean)
        depth = 0
        for line in clean.splitlines():
            directive = re.match(r"\s*#\s*(if|ifdef|ifndef|endif)\b", line)
            if directive:
                depth += -1 if directive[1] == "endif" else 1
            include = re.match(r'^\s*#\s*include\s*["<]([^">]+)[">]', line)
            if include and depth == (1 if guard else 0):
                local = (path.parent / include[1]).resolve()
                target = local if local in paths else relative_paths.get(include[1])
                if target is not None:
                    graph[path].add(target)

    def closure(path: Path, visited: set[Path]) -> set[Path]:
        if path in visited:
            return set()
        visited.add(path)
        return {path} | set().union(*(closure(child, visited) for child in graph[path]))

    covered = {path: closure(path, set()) for path in selected}
    dependencies = set().union(*(value - {path} for path, value in covered.items()))
    roots = selected - dependencies
    ordered = ordered_headers(contents)
    # Guarded include cycles still need one entry root.
    for path in ordered:
        if path in selected and not any(path in covered[root] for root in roots):
            roots.add(path)
    names = dict(headers)
    emitted: set[Path] = set()
    lines = []
    for dependency in ordered:
        for path in ordered:
            if path in roots and path not in emitted and dependency in covered[path]:
                lines.append(f'#include "{names[path]}"\n')
                emitted.add(path)
    return "".join(lines)


def _draft(
    project: Project,
    policy: Policy,
    function: str | None,
    v: str | None,
    scratch: Path,
    *,
    generation: Path | None = None,
    type_context: str = "",
    announce: bool = True,
    use_type_db: bool = True,
) -> Path:
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
    original_project = project
    work = Path(tempfile.mkdtemp(prefix=function + ".m2c.", dir=directory))
    project = draft_work.overlay(project, work)
    headers = _headers(project)
    if not use_type_db:
        from unbake.typemap.storage import generated

        headers = [(path, name) for path, name in headers if not generated(project, path)]
    context = work / "context.c"
    examples = similar.retrieve(project, function, v)
    examples_context = similar.context(examples)
    atomic_files.text(work / "similar-context.txt", examples_context, encoding="utf-8")
    # Shared headers own the types. Similar units' private declarations can
    # collide with canonical tags or leak typedefs unavailable to the draft's
    # include graph; retain those units in the similarity comments instead.
    atomic_files.text(context, _context(headers, {path for path, _ in headers}) + type_context, encoding="utf-8")
    atomic_files.text(
        context, preprocess_context(context, project, policy, v, function) + "\n" + examples_context, encoding="utf-8"
    )
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
    body = whole_body(
        canonical_entry(project, v, function, address, read_text(assembly_path, "m2c"), generation=generation), function
    )
    registers = [
        "zero",
        "at",
        "v0",
        "v1",
        "a0",
        "a1",
        "a2",
        "a3",
        "t0",
        "t1",
        "t2",
        "t3",
        "t4",
        "t5",
        "t6",
        "t7",
        "s0",
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "t8",
        "t9",
        "k0",
        "k1",
        "gp",
        "sp",
        "fp",
        "ra",
    ]
    body = re.sub(
        r"\$(\d+)\b", lambda match: "$" + registers[int(match[1])] if int(match[1]) < len(registers) else match[0], body
    )
    body = private_constants(
        project, v, function, jump_tables(project, v, function, saved_returns(body)), generation=generation
    )
    body = canonical_aliases(project, v, body, generation)
    body = delay_slots(local_targets(body), function)
    database = None
    if type_context and use_type_db:
        from unbake.typemap import load

        database = load(original_project, allow_stale=True)
        assert database is not None
    signatures = draft_abi.declarations(
        project, policy, v, body, context.read_text(), function=function, database=database
    )
    if signatures:
        with atomic_files.stream(context, "a") as stream:
            stream.write("\n" + signatures + "\n")
    atomic_files.text(assembly, register_pairs(body, compiler.cflags, function), encoding="utf-8")
    output = run_tool(
        [
            *command(executable_path, assembly.read_text()),
            "-t",
            targets[compiler.kind],
            "--valid-syntax",
            "--stack-structs",
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
    if type_context and use_type_db and "Unable to find stack arg" in output:
        from unbake.typemap.mapping import load_map

        mapped = load_map(original_project)
        item = next((item for name, item in mapped["functions"].items() if function in (name, *item["aliases"])), None)
        if item is not None:
            output = draft_abi.stack_arguments(output, context.read_text(), function, item["versions"][v])
    output, stack_header = measured_storage.prepare(project, function, output, assembly.read_text())
    if stack_header is not None:
        headers.append((stack_header, stack_header.relative_to(project.include[0]).as_posix()))
        atomic_files.text(context, context.read_text() + "\n" + stack_header.read_text())
    output = normalize(output, context.read_text())
    if "second half of f64" in output:
        raise Held("m2c", f"{function}: unresolved second half of f64 in decompiler output")
    output = stack_locals(output, context.read_text(), function, assembly.read_text())
    output = header_types(output, context.read_text())
    # Reject unsupported instructions/register reads before changing headers.
    output = lower(output, context.read_text(), allow_fields=True)
    layouts = database["structs"] if database is not None else None
    output, shared = share(project, function, output, context.read_text(), layouts=layouts)
    selected = required_headers(
        {path: read_text(path, "m2c") for path, name in headers},
        output,
    )
    atomic_files.text(context, _context(headers, selected), encoding="utf-8")
    atomic_files.text(context, preprocess_context(context, project, policy, v, function), encoding="utf-8")
    if shared is not None and shared.resolve() not in {path for path, _ in headers}:
        headers.append((shared.resolve(), shared.relative_to(project.include[0]).as_posix()))
    if shared is not None:
        selected.add(shared.resolve())
    # The draft and trial compile the same includes as a normal source unit.
    # Expanded declarations are only for m2c and layout/macro analysis.
    atomic_files.text(context, _context(headers, selected), encoding="utf-8")
    includes = context.read_text()
    declarations = preprocess_context(context, project, policy, v, function)
    atomic_files.text(context, declarations, encoding="utf-8")
    output = lower(output, declarations + "\n" + signatures)
    output = address_arithmetic(output, declarations, function)
    commands = gbi.prepare(project, output, gbi.microcode(project))
    output = commands.source
    raw_lines = output.splitlines(keepends=True)
    for item in commands.raw:
        if 0 < item.line <= len(raw_lines):
            reason = item.reason.replace("*/", "* /").replace("\n", " ")
            raw_lines[item.line - 1] = raw_lines[item.line - 1].rstrip("\n") + f" /* GBI_RAW: {reason} */\n"
    output = "".join(raw_lines)
    if type_context and re.search(r"\btypedef\b|\b(?:struct|union)\s+\w*\s*\{", output):
        raise Held("types", f"types.declaration: {function}: draft must reuse solved shared types")
    if "gbi" in commands.headers:
        includes += gbi.install(project)
    if "abi" in commands.headers:
        includes += gbi.install_audio(project)
    for item in commands.raw:
        print(f"GBI(raw): {function}:{item.line}: {item.command}: {item.reason}")
    content = (
        f"/* NON_MATCHING: draft of {function}; verify behavior and bytes before match. */\n"
        f"{includes.rstrip()}\n\n{signatures}\n\n{output.rstrip()}\n"
    )
    candidate = work / "compile-proof" / (function + ".c")
    candidate.parent.mkdir()
    atomic_files.text(candidate, content, encoding="utf-8")
    try:
        prove(project, policy, function, v, candidate)
    except Held as error:
        raise Held(
            error.phase,
            f"m2c/type compile proof failed: {error.reason}\ndraft_path: {candidate}\n"
            "Repair the shared header types identified above and redraft.",
            next_action=shlex.join(["unbake", "draft", function, "--scratch", str(directory)]),
        ) from error
    atomic_files.text(source, content, encoding="utf-8")
    draft_work.save_overlay(original_project, work)
    if announce:
        print(f"draft_path: {source}")
        print(f"source filename: {function}.c (try identifies the function from the filename)")
    return source


def draft(
    project: Project,
    policy: Policy,
    function: str | None,
    v: str | None,
    scratch: Path,
    *,
    generation: Path | None = None,
    type_context: str = "",
    announce: bool = True,
    use_type_db: bool = True,
) -> Path:
    """Name the selected function on every refusal from the draft boundary."""
    try:
        return _draft(
            project,
            policy,
            function,
            v,
            scratch,
            generation=generation,
            type_context=type_context,
            announce=announce,
            use_type_db=use_type_db,
        )
    except Held as error:
        if function and not error.reason.startswith(function + ":"):
            raise Held(
                error.phase,
                f"{function}: {error.reason}",
                next_action=error.next_action or shlex.join(["unbake", "draft", function, "--scratch", str(scratch)]),
            ) from error
        if function and error.next_action is None:
            error.next_action = shlex.join(["unbake", "draft", function, "--scratch", str(scratch)])
        raise
    except (OSError, UnicodeError) as error:
        raise Held(
            "m2c",
            f"{function}: draft input/output: {error}",
            next_action=shlex.join(["unbake", "draft", function, "--scratch", str(scratch)]) if function else None,
        ) from error
