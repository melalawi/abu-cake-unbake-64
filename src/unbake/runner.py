"""Run the generated build's commands for one unit: compile (content-keyed), place, link alone, extract .text.

The argv come from compilers.drivers (the same templates the Makefile renders). Published units write their
objects to build/<v>/src/UNIT.o (make keys its own under build/cas); drafts write under build/work/FUNC/<v>/.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache, process
from unbake.compilers import drivers
from unbake.config import Held, Host, Project
from unbake.layout import split


def tools(host: Host) -> drivers.Tools:
    return drivers.Tools(str(host.cpp), str(host.mips_as), str(host.n64link))


def object_path(project: Project, version: str, unit: str, file: Path) -> Path:
    published = project.src / f"{unit}.c"
    if file.resolve() == published.resolve():
        return project.build / version / "src" / f"{unit}.o"
    return project.work / unit / version / f"{unit}.o"


def _compiler_pins(project: Project, unit: str) -> str:
    compiler = project.compiler_for(unit)
    return cache.key(*sorted(path for path in (project.tools / compiler.id).rglob("*") if path.is_file()))


def compile_unit(
    project: Project, host: Host, file: Path, version: str, *, unit: str, non_matching: bool = False
) -> Path:
    """Compile FILE as UNIT for VERSION; the object is cached by its preprocessed text, commands and compiler."""
    file = Path(file).resolve()
    if not file.is_file():
        raise Held("compile", f"compile.source: {file}: missing file")
    source = str(file.relative_to(project.root)) if file.is_relative_to(project.root) else str(file)
    commands = drivers.steps(project, version, unit, source, tools(host), non_matching=non_matching)
    try:
        preprocessed = process.run_tool(list(commands.preprocess), project.root, "compile")
    except Held as error:
        raise Held("compile", f"compile.{Path(unit).name}: {source}: {error.reason}") from error
    absolute_cc = str(project.compiler_for(unit).cc)
    compile_argv = [absolute_cc, *commands.compile[1:]]
    content_key = cache.key(
        "object",
        preprocessed,
        "\0".join(compile_argv[1:]),
        "\0".join(commands.assemble[1:] if commands.assemble else ()),
        _compiler_pins(project, unit),
        Path(host.n64link) if commands.assemble else "",
        Path(host.mips_as) if commands.assemble else "",
    )
    name = Path(unit).name

    def make(destination: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-") as temporary:
            work = Path(temporary)
            atomic_files.text(work / f"{name}.i", preprocessed)
            process.run_tool(compile_argv, work, "compile")
            if commands.assemble is not None:
                process.run_tool(list(commands.assemble), work, "compile")
            atomic_files.copyfile(work / f"{name}.o", destination)

    try:
        cached = cache.Cache(host.cache_root).produce("object", content_key, make)
    except Held as error:
        raise Held("compile", f"compile.{name}: {source}: {error.reason}") from error
    output = object_path(project, version, unit, file)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.is_file() or output.read_bytes() != cached.read_bytes():
        atomic_files.copyfile(cached, output)
    return output


def windows(project: Project, version: str) -> list[str]:
    from unbake import buildfiles

    return [f"0x{vram:X}:0x{rom:X}:0x{size:X}" for vram, rom, size in buildfiles.windows(project, version)]


def symbols_file(project: Project, version: str) -> Path:
    path = project.root / "versions" / version / "symbols.ld"
    if not path.is_file():
        raise Held("compile", f"build.symbols: {path} is missing; run unbake recompute buildfiles")
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
    if kind not in drivers.UNTRIMMED:
        argv.append("--trim")
    if score:
        argv.append("--score")
    result = subprocess.run(argv, cwd=project.root, capture_output=True, text=True)
    if result.returncode:
        raise Held("place", f"n64link place exited {result.returncode}: {result.stderr.strip()}")
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
            "link",
            f"link.undefined: {source}: VERSION {version}: {', '.join(unknown)} "
            f"in neither versions/{version}/symbols.ld nor an address name",
        )
    return [f"--defsym={name}=0x{buildfiles.named_address(name):08X}" for name in missing]


def link(
    project: Project, host: Host, placed: Path, version: str, row: split.Function, work: Path, source: Path
) -> bytes:
    """Link the placed object alone at its address and return its .text bytes; a link failure is a refusal
    naming SOURCE."""
    elf = work / "unit.elf"
    binary = work / "unit.bin"
    script = project.root / "versions" / version / f"{project.name}.ld"
    if not script.is_file():
        raise Held("compile", f"build.link_script: {script} is missing; run unbake recompute buildfiles")
    derived = derived_symbols(undefined(placed), provided(symbols_file(project, version)), version, source)
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
        )
    except Held as error:
        raise Held("link", f"link.failed: {source}: VERSION {version}: {error.reason}") from error
    process.run_tool(
        [str(host.mips_objcopy), "-O", "binary", "-j", ".text", str(elf), str(binary)], project.root, "link"
    )
    return binary.read_bytes()


def link_function(
    project: Project, host: Host, obj: Path, version: str, row: split.Function, source: Path
) -> tuple[bytes, list[str]]:
    """Score mode: the unit's linked words even when some constants are unproved, with those problems."""
    with tempfile.TemporaryDirectory(prefix="link-") as temporary:
        work = Path(temporary)
        placed = work / "placed.o"
        problems = place(project, host, obj, version, row, placed, score=True)
        return link(project, host, placed, version, row, work, source), problems


def build_unit(project: Project, host: Host, unit: str, version: str) -> bytes:
    """Strict mode (land): compile src/UNIT.c, prove every constant, link; refuse on any unproved constant."""
    rows = [row for row in split.functions(project, version) if Path(row.path).name == unit and row.kind == "c"]
    if len(rows) != 1:
        raise Held("build", f"build.row: {unit}: expected one c row in VERSION {version}, found {len(rows)}")
    row = rows[0]
    obj = compile_unit(project, host, project.src / f"{unit}.c", version, unit=unit)
    with tempfile.TemporaryDirectory(prefix="build-") as temporary:
        work = Path(temporary)
        placed = work / "placed.o"
        place(project, host, obj, version, row, placed, score=False)
        data = link(project, host, placed, version, row, work, project.src / f"{unit}.c")
    if len(data) != row.end - row.start:
        raise Held("build", f"build.size: {unit} {version}: 0x{len(data):X} bytes for a 0x{row.end - row.start:X} row")
    return data


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def preprocess(project: Project, host: Host, file: Path, version: str, *, unit: str) -> str:
    """The unit's preprocessed text exactly as compile_unit sees it."""
    file = Path(file).resolve()
    source = str(file.relative_to(project.root)) if file.is_relative_to(project.root) else str(file)
    commands = drivers.steps(project, version, unit, source, tools(host))
    return process.run_tool(list(commands.preprocess), project.root, "compile")
