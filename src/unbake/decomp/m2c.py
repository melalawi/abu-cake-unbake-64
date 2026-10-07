"""Generate one C draft using the configured m2c and project header context."""

from __future__ import annotations

import re
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cdecl, tui
from unbake.compilers import drivers
from unbake.config import Held, Host, Project
from unbake.decomp import draft_abi, gbi, measured_storage, similar
from unbake.decomp.draft_asm import delay_slots, local_targets, saved_returns
from unbake.decomp.draft_compile import prove
from unbake.decomp.draft_context import ordered_headers, preprocess_context, required_headers
from unbake.decomp.draft_fp import command
from unbake.decomp.draft_input import (
    assembly_source,
    canonical_aliases,
    canonical_entry,
    header_types,
    jump_tables,
    private_constants,
    stack_locals,
    whole_body,
)
from unbake.decomp.draft_layouts import access_widths, normalize
from unbake.decomp.draft_macros import lower
from unbake.decomp.draft_syntax import address_arithmetic
from unbake.decomp.field_access import share
from unbake.process import capture, read_text, run_tool
from unbake.process import named as cause_named
from unbake.project.headers import include_headers


def _shared_type_gate(output: str, function: str) -> None:
    """Local storage overlays do not introduce a shared aggregate contract."""
    # Blank implementation bodies, retaining signatures and global initializers.
    # The same declaration boundary is used by canonical ABI extraction.
    from unbake.typemap.declarations import cleaned_unit

    source = cdecl.declaration_source(output)
    source = cdecl.SOURCE_TOKEN.sub(lambda m: " " if m[0].startswith(('"', "'")) else m[0], source)
    declarations = cleaned_unit(source)
    if re.search(r"\btypedef\b|\b(?:struct|union)\s+\w*\s*\{", declarations):
        raise Held(
            cause_named(
                "types.declaration",
                f"types.declaration: {function}: draft must reuse solved shared types",
                owner="decomp.m2c",
                stage="types",
            )
        )


def _headers(project: Project) -> list[tuple[Path, str]]:
    if not project.include:
        raise Held(
            cause_named("decomp.m2c._headers", "paths.include is missing or empty", owner="decomp.m2c", stage="m2c")
        )
    for directory in project.include:
        if not Path(directory).is_dir():
            raise Held(
                cause_named(
                    "decomp.m2c._headers",
                    f"paths.include directory {directory} is missing",
                    owner="decomp.m2c",
                    stage="m2c",
                )
            )
    headers = include_headers(project)
    if not headers:
        raise Held(
            cause_named(
                "decomp.m2c._headers",
                f"paths.include {project.include}: project headers are missing",
                owner="decomp.m2c",
                stage="m2c",
            )
        )
    return headers


def _context(headers: list[tuple[Path, str]], selected: set[Path]) -> str:
    paths = {path for path, _ in headers}
    contents = {path: read_text(path, "m2c") for path, _ in headers}
    relative_paths: dict[str, Path] = {}
    for path, relative in headers:
        if relative in relative_paths and relative_paths[relative] != path:
            raise Held(
                cause_named(
                    "decomp.m2c._context",
                    f"paths.include has ambiguous header {relative}",
                    owner="decomp.m2c",
                    stage="m2c",
                )
            )
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
    policy: Host,
    function: str,
    v: str,
    work: Path,
    extracted: Path,
    *,
    type_context: str,
    use_type_db: bool,
) -> str:
    """Draft text for one function. project is the draft view (its own include dir first)."""
    if not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held(
            cause_named(
                "decomp.m2c._draft", "function is required and must be a C identifier", owner="decomp.m2c", stage="m2c"
            )
        )
    project.version(v)
    assembly_text, address = assembly_source(project, v, function, extracted)
    executable_path = str(policy.m2c)
    from unbake.compilers.families import family_for
    from unbake.compilers.registry import specification

    compiler = project.compiler_for(project.src / (function + ".c"))
    target = specification(compiler.id).m2c
    if not target:
        raise Held(
            cause_named(
                f"compiler.{compiler.id}",
                f"compiler.{compiler.id}: no registered decompiler target",
                owner="decomp.m2c",
                stage="m2c",
            )
        )
    family = family_for(compiler)
    original_project = project
    work.mkdir(parents=True, exist_ok=True)
    headers = _headers(project)
    if not use_type_db:
        from unbake.typemap.storage import generated

        headers = [(path, name) for path, name in headers if not generated(project, path)]
    context = work / "context.c"
    examples = similar.retrieve(project, function, v, extracted)
    examples_context = similar.context(examples)
    atomic_files.text(work / "similar-context.txt", examples_context, encoding="utf-8")
    # Shared headers own the types. Similar units' private declarations can
    # collide with canonical tags or leak typedefs unavailable to the draft's
    # include graph; retain those units in the similarity comments instead.
    # The first context holds only the declarations the target and the solved context name (and what those
    # need), not every project header: a draft's parsers and caches then scale with the function.
    contents = {path: read_text(path, "m2c") for path, _ in headers}
    needed = required_headers(contents, f"{function}\n{assembly_text}\n{type_context}")
    del contents
    atomic_files.text(context, _context(headers, needed) + type_context, encoding="utf-8")
    atomic_files.text(
        context, preprocess_context(context, project, policy, v, function) + "\n" + examples_context, encoding="utf-8"
    )
    assembly = work / (function + ".s")
    body = whole_body(canonical_entry(project, v, function, address, assembly_text, generation=extracted), function)
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
        project, v, function, jump_tables(project, v, function, saved_returns(body)), generation=extracted
    )
    body = canonical_aliases(project, v, body, extracted)
    body = delay_slots(local_targets(body), function)
    types_path = None
    if type_context and use_type_db:
        from unbake.typemap import types_db

        types_path = types_db.path(original_project)
        if not types_path.is_file():
            raise Held(
                cause_named(
                    "types.database",
                    f"types.database: {types_path} is missing; the types step builds it",
                    owner="decomp.m2c",
                    stage="draft",
                )
            )
    signatures = draft_abi.declarations(
        project, policy, v, body, context.read_text(), function=function, types_path=types_path
    )
    if signatures:
        with atomic_files.stream(context, "a") as stream:
            stream.write("\n" + signatures + "\n")
    atomic_files.text(
        assembly, family.m2c_registers(body, tuple(drivers.flags(project, v, function)), function), encoding="utf-8"
    )
    output = run_tool(
        [
            *command(executable_path, assembly.read_text()),
            "-t",
            target,
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
        raise Held(
            cause_named(
                "decomp.m2c._draft",
                f"policy.m2c {executable_path} produced no draft for {function}",
                owner="decomp.m2c",
                stage="m2c",
            )
        )
    if type_context and use_type_db and "Unable to find stack arg" in output:
        mapped = draft_abi.mapped_body(original_project, function, v)
        if mapped is not None:
            output = draft_abi.stack_arguments(output, context.read_text(), function, mapped)
    output, stack_header = measured_storage.prepare(project, function, output, assembly.read_text())
    if stack_header is not None:
        headers.append((stack_header, stack_header.relative_to(project.include[0]).as_posix()))
        atomic_files.text(context, context.read_text() + "\n" + stack_header.read_text())
    output = normalize(output, context.read_text(), access_widths(assembly.read_text()))
    if "second half of f64" in output:
        raise Held(
            cause_named(
                f"{function}",
                f"{function}: unresolved second half of f64 in decompiler output",
                owner="decomp.m2c",
                stage="m2c",
            )
        )
    output = stack_locals(output, context.read_text(), function, assembly.read_text())
    output = header_types(output, context.read_text())
    # Reject unsupported instructions/register reads before changing headers.
    output = lower(output, context.read_text(), allow_fields=True)
    output, shared = share(project, function, output, context.read_text(), types_path=types_path)
    contents = {path: read_text(path, "m2c") for path, name in headers}
    selected = required_headers(contents, output)
    atomic_files.text(context, _context(headers, selected), encoding="utf-8")
    atomic_files.text(context, preprocess_context(context, project, policy, v, function), encoding="utf-8")
    if shared is not None and shared.resolve() not in {path for path, _ in headers}:
        headers.append((shared.resolve(), shared.relative_to(project.include[0]).as_posix()))
        contents[shared.resolve()] = read_text(shared, "m2c")
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
    if type_context:
        _shared_type_gate(output, function)
    if "gbi" in commands.headers:
        includes += gbi.install(project)
    if "abi" in commands.headers:
        includes += gbi.install_audio(project)
    for item in commands.raw:
        tui.line(f"GBI(raw): {function}:{item.line}: {item.command}: {item.reason}")
    content = (
        f"/* NON_MATCHING: draft of {function}; verify behavior and bytes before match. */\n"
        f"{includes.rstrip()}\n\n{signatures}\n\n{output.rstrip()}\n"
    )
    from unbake.typemap import namespace

    contracts = namespace.project_declarations(project, contents, texts=(content,))
    content = contracts.rewrite(content)
    candidate = work / "compile-proof" / (function + ".c")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    atomic_files.text(candidate, content, encoding="utf-8")
    try:
        with namespace.comparison_view(project, policy, candidate, content, contents=contents, contracts=contracts) as (
            view,
            compiled,
        ):
            prove(view, policy, function, v, compiled)
    except Held as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "decomp.m2c._draft",
                    f"m2c/type compile proof failed: {error.reason}",
                    owner="decomp.m2c",
                    stage=error.phase,
                ),
            )
        ) from error
    return content


def draft(
    project: Project,
    policy: Host,
    function: str,
    version: str,
    work: Path,
    extracted: Path,
    *,
    type_context: str,
    use_type_db: bool = True,
) -> str:
    """Name the selected function on every refusal from the draft boundary."""
    try:
        return _draft(
            project, policy, function, version, work, extracted, type_context=type_context, use_type_db=use_type_db
        )
    except Held as error:
        if not error.reason.startswith(function + ":"):
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        f"{function}",
                        f"{function}: {error.reason}",
                        owner="decomp.m2c",
                        stage=error.phase,
                        action=error.fault.cause.action,
                    ),
                )
            ) from error
        raise
    except (OSError, UnicodeError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    f"{function}", f"{function}: draft input/output: {error}", owner="decomp.m2c", stage="m2c"
                ),
            )
        ) from error
