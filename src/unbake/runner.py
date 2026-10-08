"""Run the generated build's commands for one unit: compile (content-keyed), place, link alone, extract .text.

The argv come from compilers.drivers (the same templates the Makefile renders).
Each compile consumer owns a temporary object until its context ends. Concurrent
sources for the same unit/version cannot overwrite one another's link input.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from unbake import atomic as atomic_files
from unbake import cache, inputs, process, scratch
from unbake import cache as retention
from unbake.compilers import drivers
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.objects.rodata import InitializedSection


def tools(host: Host) -> drivers.Tools:
    return drivers.Tools(str(host.cpp), str(host.mips_as), str(host.n64link))


def _compiler_pins(project: Project, unit: str) -> str:
    """The compiler tree's file digests; each digest is reused while the file's stat signature holds."""
    compiler = project.compiler_for(unit)
    root = project.tools / compiler.id
    files = sorted(path for path in root.rglob("*") if path.is_file())
    return cache.key(
        *(
            part
            for path in files
            for part in (
                path.relative_to(root).as_posix(),
                inputs.digest(path, algorithm="sha256", reuse=retention.configured()),
            )
        )
    )


@contextmanager
def compile_unit(
    project: Project,
    host: Host,
    file: Path,
    version: str,
    *,
    unit: str,
    non_matching: bool = False,
    verify_input: Callable[[str], None] | None = None,
) -> Iterator[Path]:
    """Own one object lifetime; reuse the CAS by preprocessed text, commands and compiler."""
    file = Path(file).resolve()
    if not file.is_file():
        raise Held(
            cause_named("compile.source", f"compile.source: {file}: missing file", owner="runner", stage="compile")
        )
    source = str(file.relative_to(project.root)) if file.is_relative_to(project.root) else str(file)
    commands = drivers.steps(project, version, unit, source, tools(host), non_matching=non_matching)
    from unbake.report import data as data_evidence

    compiler_pins = _compiler_pins(project, unit)
    captured = (
        data_evidence.capture_inputs(
            project, file, version, list(commands.preprocess), compiler=compiler_pins, non_matching=non_matching
        )
        if file.is_relative_to(project.src)
        else None
    )
    linked_tools = data_evidence.link_tools(host) if captured is not None else None
    try:
        preprocessed = drivers.run_preprocess(
            project,
            list(commands.preprocess),
            "compile",
            unit=unit,
            context={"source": str(file), "function": unit, "version": version},
        )
    except Held as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    f"compile.{Path(unit).name}",
                    f"compile.{Path(unit).name}: {source}: {error.reason}",
                    owner="runner",
                    stage="compile",
                ),
            )
        ) from error
    if verify_input is not None:
        verify_input(preprocessed)
    absolute_cc = str(project.compiler_for(unit).cc)
    compile_argv = [absolute_cc, *commands.compile[1:]]
    content_key = cache.key(
        "object",
        preprocessed,
        "\0".join(compile_argv[1:]),
        "\0".join(commands.assemble[1:] if commands.assemble else ()),
        compiler_pins,
        inputs.digest(Path(host.n64link), algorithm="sha256", reuse=retention.configured())
        if commands.assemble
        else "",
        inputs.digest(Path(host.mips_as), algorithm="sha256", reuse=retention.configured())
        if commands.assemble
        else "",
    )
    name = Path(unit).name

    def make(destination: Path) -> None:
        with scratch.temporary(host, project, "compile", prefix="compile-") as temporary:
            work = Path(temporary)
            atomic_files.text(work / f"{name}.i", preprocessed)
            process.run_tool(
                compile_argv, work, "compile", context={"source": str(file), "function": unit, "version": version}
            )
            if commands.assemble is not None:
                process.run_tool(
                    list(commands.assemble),
                    work,
                    "compile",
                    context={"source": str(file), "function": unit, "version": version},
                )
            atomic_files.copyfile(work / f"{name}.o", destination)

    with scratch.temporary(host, project, "compile", prefix="object-") as temporary:
        output = Path(temporary) / f"{name}.o"
        try:
            cached = cache.Cache(project.cache).produce("object", content_key, make)
            atomic_files.copyfile(cached, output)
        except Held as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        f"compile.{name}", f"compile.{name}: {source}: {error.reason}", owner="runner", stage="compile"
                    ),
                )
            ) from error
        except OSError as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "compile.object",
                        f"compile.object: {source}: object could not be materialized: {error}",
                        owner="runner",
                        stage="compile",
                    ),
                )
            ) from error
        if captured is not None:
            data_evidence.assert_inputs(project, captured)
            captured = inputs.DependencySet(
                captured.files,
                {
                    **captured.values,
                    "object_sha256": inputs.digest(output, algorithm="sha256", reuse=False),
                    "link_tools": linked_tools,
                },
                captured.recipes,
            )
            atomic_files.text(output.with_suffix(".inputs.json"), json.dumps(captured.document()))
        yield output


def windows(project: Project, version: str) -> list[str]:
    from unbake import buildfiles

    return [f"0x{vram:X}:0x{rom:X}:0x{size:X}" for vram, rom, size in buildfiles.windows(project, version)]


def symbols_file(project: Project, version: str) -> Path:
    path = project.root / "versions" / version / "symbols.ld"
    if not path.is_file():
        raise Held(
            cause_named(
                "build.symbols",
                f"build.symbols: {path} is missing; run unbake recompute buildfiles",
                owner="runner",
                stage="compile",
            )
        )
    return path


def place(
    project: Project, host: Host, obj: Path, version: str, row: split.Function, output: Path, *, score: bool
) -> list[str]:
    """Run n64link place; with score, unproved constants come back as problems instead of a refusal."""
    kind = project.compiler_for(Path(obj).stem).kind
    argv = [
        str(host.n64link),
        "place",
        str(obj),
        "-o",
        str(output),
        "--rom",
        str(project.version(version).baserom),
        "--text",
        f"0x{row.address:X}:0x{row.start:X}:0x{row.end - row.start:X}",
        *(item for window in windows(project, version) for item in ("--map", window)),
        "--symbols",
        str(symbols_file(project, version)),
    ]
    if row.kind == "hasm" or not drivers.preserves_padding(kind):
        argv.append("--trim")
    if score:
        argv.append("--score")
    result = process.run_native(
        argv,
        project.root,
        "place",
        context={"function": row.name, "version": version, "address": row.address, "source": str(obj)},
    )
    prefix = "n64link: place: unproved: "
    return [line[len(prefix) :] for line in result.stderr.splitlines() if line.startswith(prefix)]


_PROVIDED = re.compile(r"^PROVIDE\((\w+) = ", re.M)


def provided(path: Path) -> frozenset[str]:
    """The names a version's symbols.ld provides."""
    return cache.parsed("runner.provided", path, lambda: frozenset(_PROVIDED.findall(path.read_text())))


def undefined(placed: Path) -> set[str]:
    """The strong global symbols the placed object references but does not define."""
    from unbake.objects import elf

    return {
        symbol["name"]
        for symbols in elf.Object(placed).symbols.values()
        for symbol in symbols
        if symbol["section"] == 0 and symbol["name"] and symbol["info"] >> 4 == 1
    }


def derived_symbols(names: set[str], known: frozenset[str], version: str, source: Path) -> list[str]:
    """--defsym for each missing address-named symbol at the address its name encodes (the rule symbols.ld
    applies to published C); refused naming the source and every other missing symbol."""
    from unbake import buildfiles

    missing = sorted(names - known)
    unknown = [name for name in missing if buildfiles.named_address(name) is None]
    if unknown:
        raise Held(
            cause_named(
                "link.undefined",
                (
                    f"link.undefined: {source}: VERSION {version}: {', '.join(unknown)} "
                    f"in neither versions/{version}/symbols.ld nor an address name"
                ),
                owner="runner",
                stage="link",
            )
        )
    return [f"--defsym={name}=0x{buildfiles.named_address(name):08X}" for name in missing]


def initialized_layout(
    project: Project, original: Path, placed: Path, source: Path, version: str
) -> tuple[InitializedSection, ...]:
    from unbake.objects import rodata
    from unbake.objects.elf import Object
    from unbake.project.headers import Graph

    sizes: dict[str, int] = {}
    obj = Object(original)
    has_labels = any(
        symbol["info"] & 15 == 0
        and symbol["name"]
        and 0 < symbol["section"] < len(obj.sections)
        and obj.sections[symbol["section"]][1] == 1
        and not obj.sections[symbol["section"]][2] & 4
        for table in obj.symbols.values()
        for symbol in table
    )
    if has_labels and source.is_relative_to(project.src):
        Graph.capture(project).initialized_definitions(project, source, version, sizes=sizes)
    return rodata.initialized_sections(
        obj,
        Object(placed),
        tuple(tuple(int(part, 16) for part in value.split(":")) for value in windows(project, version)),
        definition_sizes=sizes,
    )


def link(
    project: Project,
    host: Host,
    placed: Path,
    version: str,
    row: split.Function,
    work: Path,
    source: Path,
    original: Path | None = None,
    *,
    score: bool = False,
) -> bytes:
    """Link the placed object alone at its address and return its .text bytes; a link failure is a refusal
    naming SOURCE."""
    elf = work / "unit.elf"
    binary = work / "unit.bin"
    script = project.root / "versions" / version / f"{project.name}.ld"
    if not script.is_file():
        raise Held(
            cause_named(
                "build.link_script",
                f"build.link_script: {script} is missing; run unbake recompute buildfiles",
                owner="runner",
                stage="compile",
            )
        )
    from unbake.objects import rodata
    from unbake.objects.elf import Object

    layout = initialized_layout(project, original, placed, source, version) if original is not None else ()
    sections = [
        name for name in rodata.unresolved_sections(Object(placed)) if name not in {item.name for item in layout}
    ]
    if layout:
        process.run_tool(
            [
                str(host.mips_objcopy),
                *(word for item in layout for word in ("--set-section-flags", item.name + "=alloc,load,data")),
                str(placed),
            ],
            project.root,
            "link",
            context={"source": str(source), "version": version},
        )
        declarations = "\n".join(
            f'  {item.output_name} 0x{item.address:08X} : SUBALIGN(1) {{ *("{item.name}") }}' for item in layout
        )
        emitted_script = work / "emitted.ld"
        atomic_files.text(emitted_script, rodata.insert_fragment(script.read_text(), declarations))
        script = emitted_script
    if sections:
        if not score:
            raise Held(
                cause_named(
                    "link.unproved",
                    f"link.unproved: {source}: VERSION {version}: unplaced {', '.join(sections)}",
                    owner="runner",
                    stage="link",
                )
            )
        trial = work / "trial.ld"
        atomic_files.text(trial, rodata.insert_fragment(script.read_text(), rodata.trial_fragment(sections)))
        script = trial
    missing = undefined(placed)
    known = provided(symbols_file(project, version))
    from unbake.compilers.runtime import bindings

    runtime = bindings(project, version) if missing - known else {}
    derived = derived_symbols(missing, known | frozenset(runtime), version, source)
    derived.extend(f"--defsym={name}=0x{runtime[name]:08X}" for name in sorted(missing - known) if name in runtime)
    try:
        process.run_tool(
            [
                str(host.mips_ld),
                "-EB",
                "-T",
                str(script),
                f"--section-start=.text=0x{row.address:X}",
                *derived,
                "-o",
                str(elf),
                str(placed),
            ],
            project.root,
            "link",
            context={"source": str(source), "function": row.name, "version": version, "address": row.address},
        )
    except Held as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "link.failed",
                    f"link.failed: {source}: VERSION {version}: {error.reason}",
                    owner="runner",
                    stage="link",
                ),
            )
        ) from error
    process.run_tool(
        [str(host.mips_objcopy), "-O", "binary", "-j", ".text", str(elf), str(binary)],
        project.root,
        "link",
        context={"source": str(source), "function": row.name, "version": version, "address": row.address},
    )
    linked = binary.read_bytes()
    if layout and original is not None and source.is_relative_to(project.src) and linked == split.words(project, row):
        from unbake.report import data

        data.record_linked(project, host, source, version, original, elf, layout)
    return linked


def link_function(
    project: Project, host: Host, obj: Path, version: str, row: split.Function, source: Path
) -> tuple[bytes, list[str]]:
    """Score mode: the unit's linked words even when some constants are unproved, with those problems."""
    from unbake.objects import rodata
    from unbake.objects.elf import Object

    with scratch.temporary(host, project, "compile", prefix="link-") as temporary:
        work = Path(temporary)
        placed = work / "placed.o"
        problems = place(project, host, obj, version, row, placed, score=True)
        # Even if native placement omits a diagnostic, placeholder addresses
        # must never make this trial eligible for an exact comparison.
        layout = initialized_layout(project, obj, placed, source, version)
        emitted = {item.name for item in layout}
        problems.extend(
            f"{section}: no proved resident address"
            for section in rodata.unresolved_sections(Object(placed))
            if section not in emitted
        )
        return link(project, host, placed, version, row, work, source, obj, score=True), problems


def build_unit(project: Project, host: Host, unit: str, version: str, *, source: Path | None = None) -> bytes:
    """Strict mode (land): compile src/UNIT.c, prove every constant, link; refuse on any unproved constant."""
    rows = [row for row in split.functions(project, version) if Path(row.path).name == unit and row.kind == "c"]
    if len(rows) != 1:
        raise Held(
            cause_named(
                "build.row",
                f"build.row: {unit}: expected one c row in VERSION {version}, found {len(rows)}",
                owner="runner",
                stage="build",
            )
        )
    row = rows[0]
    file = project.src / f"{unit}.c" if source is None else source
    with (
        compile_unit(project, host, file, version, unit=unit) as obj,
        scratch.temporary(host, project, "compile", prefix="build-") as temporary,
    ):
        work = Path(temporary)
        placed = work / "placed.o"
        place(project, host, obj, version, row, placed, score=False)
        data = link(project, host, placed, version, row, work, file, obj)
    if len(data) != row.end - row.start:
        raise Held(
            cause_named(
                "build.size",
                f"build.size: {unit} {version}: 0x{len(data):X} bytes for a 0x{row.end - row.start:X} row",
                owner="runner",
                stage="build",
            )
        )
    return data


def preprocess(project: Project, host: Host, file: Path, version: str, *, unit: str) -> str:
    """The unit's preprocessed text exactly as compile_unit sees it."""
    file = Path(file).resolve()
    source = str(file.relative_to(project.root)) if file.is_relative_to(project.root) else str(file)
    commands = drivers.steps(project, version, unit, source, tools(host))
    return drivers.run_preprocess(
        project,
        list(commands.preprocess),
        "compile",
        unit=unit,
        context={"source": str(file), "function": unit, "version": version},
    )


def dependencies(
    project: Project, host: Host, file: Path, version: str, *, unit: str, non_matching: bool = False
) -> set[Path]:
    """Native preprocessing prerequisites with the same effective unit/version flags."""
    from unbake.compilers.families import family_for

    argv = drivers.preprocess_command(project, str(host.cpp), version, unit, file, non_matching=non_matching)
    argv = family_for(project.compiler_for(unit)).dependency_command(argv)
    output = drivers.run_preprocess(
        project, argv, "compile", unit=unit, context={"source": str(file), "function": unit, "version": version}
    )
    paths = {
        Path(os.path.abspath(project.root / name))
        for name in family_for(project.compiler_for(unit)).dependency_paths(output)
    }
    if file.absolute() not in paths:
        raise Held(
            cause_named(
                "compile.dependencies",
                f"compile.dependencies: {unit} {version}: proved source missing from native rule",
                owner="runner",
                stage="compile",
            )
        )
    return paths - {file.absolute()}
