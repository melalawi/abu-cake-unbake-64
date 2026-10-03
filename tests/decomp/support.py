"""Trial checks with isolated project fixtures and explicit MIPS ELF tables."""

import hashlib
import struct
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from tests.support import tool
from unbake.project.config import Compiler, Policy, Project, Version, load_policy


class FixturePolicy(SimpleNamespace):
    __dataclass_fields__ = Policy.__dataclass_fields__


SCRATCH_ROOT = Path(tempfile.gettempdir()).resolve()
ASSEMBLER = cast(Callable[[str], str], tool)("mips-linux-gnu-as")
LINKER = cast(Callable[[str], str], tool)("mips-linux-gnu-ld")
READELF = cast(Callable[[str], str], tool)("mips-linux-gnu-readelf")
OBJDUMP = cast(Callable[[str], str], tool)("mips-linux-gnu-objdump")


def assemble(directory: Path, name: str, text: str) -> Path:
    source = directory / (name + ".s")
    output = directory / (name + ".o")
    source.write_text(text, encoding="utf-8")
    from tests.assembly_fixture import object_fixture

    return object_fixture(output, text)


def assembly(function: str, words: list[int]) -> str:
    return (
        f".set noreorder\n.text\n.balign 4\n.globl {function}\n.type {function}, @function\n{function}:\n"
        + "".join(f".word 0x{word:08X}\n" for word in words)
        + f".size {function}, .-{function}\n"
    )


def fixture(
    directory: Path, words: list[int] | None = None, versions: tuple[str, ...] = ("us",), *, case
) -> tuple[Project, SimpleNamespace, Path]:
    from tests.process_fakes import boundary, copy
    from unbake.match import staging

    copying = boundary(staging, copy)
    copying.start()
    case.addCleanup(copying.stop)
    from tests.preprocessor import output
    from tests.process_fakes import boundary, script_output
    from unbake.decomp import trial_compile
    from unbake.typemap import declarations

    def frontend(command, **kwargs):
        import subprocess

        if Path(command[0]).name == "fixture-cc" or (hasattr(case, "m2c_output") and Path(command[0]).name == "cc"):
            if getattr(case, "compiler_error", ""):
                return subprocess.CompletedProcess(command, 1, "", case.compiler_error)
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_bytes(b"compiler output fixture")
            return subprocess.CompletedProcess(command, 0, "", "")
        if "cpp" in Path(command[0]).name:
            try:
                return output(command, **kwargs)
            except ValueError as error:
                return subprocess.CompletedProcess(command, 1, "", str(error))
        if hasattr(case, "m2c_output") and Path(command[0]).resolve() == load_policy().m2c.resolve():
            return subprocess.CompletedProcess(command, 0, case.m2c_output, "")
        return script_output(command, **kwargs)

    process = boundary(trial_compile, frontend)
    process.start()
    case.addCleanup(process.stop)
    from unbake.layout import structs

    preprocessing = boundary(structs, output)
    preprocessing.start()
    case.addCleanup(preprocessing.stop)
    mock = boundary(declarations, output)
    mock.start()
    case.addCleanup(mock.stop)
    words = [0x24020001, 0x03E00008, 0] if words is None else words
    root = directory / "project"
    root.mkdir()
    for relative in ("src", "include", "asm", "tools", "build"):
        (root / relative).mkdir()
    (root / "config.toml").write_text(
        '[build]\nld="ld"\nobjcopy="objcopy"\nsplat="splat"\nas="as"\nasflags=[]\n'
        'cpp="policy:cpp"\ncppflags=[]\nsn64_asflags=[]\n'
    )
    (root / "include" / "types.h").write_text("typedef int s32;\n", encoding="utf-8")
    version_map = {}
    for v in versions:
        definitions = root / "versions" / v
        definitions.mkdir(parents=True)
        start = 0x80001000 if v == "us" else 0x80202000
        next_offset = 0x40 + len(words) * 4
        split = definitions / "game.yaml"
        split.write_text(
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            f"  - name: main\n    type: code\n    start: 0x40\n    vram: 0x{start:X}\n"
            "    subalign: 4\n    subsegments:\n"
            f"      - [0x40, asm, nonmatchings/alpha]\n      - [0x{next_offset:X}, asm, beta]\n"
            f"      - [0x{next_offset + 12:X}, asm, gamma]\n  - [0x{next_offset + 24:X}]\n",
            encoding="utf-8",
        )
        symbols = definitions / "symbol_addrs.txt"
        symbols.write_text(
            f"alpha = 0x{start:X};\nbeta = 0x{start + len(words) * 4:X};\ngamma = 0x{start + len(words) * 4 + 12:X};\n",
            encoding="utf-8",
        )
        baserom = root / f"baserom.{v}.z64"
        all_words = [*words, 0x24020002, 0x03E00008, 0, 0x24020003, 0x03E00008, 0]
        baserom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + struct.pack(f">{len(all_words)}I", *all_words))
        generation = root / "build" / f"{v}.generation"
        generation.mkdir()
        program = "\n".join(
            assembly(f, w)
            for f, w in (
                ("alpha", words),
                ("beta", [0x24020002, 0x03E00008, 0]),
                ("gamma", [0x24020003, 0x03E00008, 0]),
            )
        )
        objects = generation / "obj/asm/nonmatchings"
        objects.mkdir(parents=True)
        unit = assemble(objects, "alpha", program)
        (generation / ".split.mk").touch()
        (generation / ".inuse").touch()
        (generation / "fixture.ld").write_text("SECTIONS { .text : { obj/asm/nonmatchings/alpha.o(.text) } }\n")
        script = generation / "layout.ld"
        script.write_text(
            f"external = 0x80003000;\nSECTIONS {{ .text 0x{start:X} : {{ *(.text) }} "
            "/DISCARD/ : { *(.reginfo) *(.MIPS.abiflags) *(.pdr) } }\n",
            encoding="utf-8",
        )
        (generation / "game.elf").write_bytes(unit.read_bytes())
        (root / "build" / v).symlink_to(generation.name, target_is_directory=True)
        version_map[v] = Version(v, baserom, hashlib.sha1(baserom.read_bytes()).hexdigest(), split, symbols, ())
        asm = root / "asm" / v / "nonmatchings"
        asm.mkdir(parents=True)
        (asm / "alpha.s").write_text(assembly("alpha", words), encoding="utf-8")
    frontend = root / "tools" / "fixture-cc"
    frontend.write_bytes(b"fake compiler")
    frontend.chmod(0o755)
    (root / "tools/compiler.sha256").write_text("fixture compiler pins\n")
    assembler = root / "tools" / "fixture-as"
    assembler.write_bytes(b"fixture assembler")
    compiler = Compiler("ido-7.1", "ido", frontend, assembler, (), root / "tools" / "compiler.sha256")
    project = Project(
        root,
        "fixture",
        "Fixture",
        versions[0],
        tuple(versions),
        root / "src",
        (root / "include",),
        root / "asm",
        root / "tools",
        {"ido-7.1": compiler},
        "ido-7.1",
        {},
        version_map,
        id="00000000-0000-4000-8000-000000000001",
        workspace_id="00000000-0000-4000-8000-000000000002",
        roms=root / "roms",
        build=root / "build",
        work=directory / "work",
        drafts=root / "build/drafts",
    )
    configured = load_policy()
    from unbake.project.config import Policy

    policy = FixturePolicy(**vars(configured))
    policy.mips_ld = Path(LINKER)
    policy.mips_readelf = Path(READELF)
    policy.mips_objdump = Path(OBJDUMP)
    policy.mips_as = assembler
    policy.cpp = root / "tools" / "fixture-cpp"
    policy.cpp.write_bytes(b"fixture preprocessor")
    policy.cppflags = ()
    policy.__dataclass_fields__ = Policy.__dataclass_fields__
    source = directory / "alpha.c"
    source.write_text("/* NON_MATCHING: returns one. */\nint alpha(void) { return 1; }\n", encoding="utf-8")
    return project, policy, source
