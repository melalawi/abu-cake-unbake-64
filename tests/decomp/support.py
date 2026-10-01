"""Trial checks with isolated project fixtures and real MIPS ELF linking."""

import hashlib
import json
import struct
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from tests.support import tool
from unbake.project.config import Compiler, Project, Version, load_policy

SCRATCH_ROOT = Path(tempfile.gettempdir())
ASSEMBLER = cast(Callable[[str], str], tool)("mips-linux-gnu-as")
LINKER = cast(Callable[[str], str], tool)("mips-linux-gnu-ld")
READELF = cast(Callable[[str], str], tool)("mips-linux-gnu-readelf")
OBJDUMP = cast(Callable[[str], str], tool)("mips-linux-gnu-objdump")


def assemble(directory: Path, name: str, text: str) -> Path:
    source = directory / (name + ".s")
    output = directory / (name + ".o")
    source.write_text(text, encoding="utf-8")
    subprocess.run(
        [ASSEMBLER, "-EB", "-mips3", "--no-pad-sections", "-o", str(output), str(source)],
        check=True,
        capture_output=True,
    )
    return output


def assembly(function: str, words: list[int]) -> str:
    return (
        f".set noreorder\n.text\n.balign 4\n.globl {function}\n.type {function}, @function\n{function}:\n"
        + "".join(f".word 0x{word:08X}\n" for word in words)
        + f".size {function}, .-{function}\n"
    )


def fixture(
    directory: Path, words: list[int] | None = None, versions: tuple[str, ...] = ("us",)
) -> tuple[Project, SimpleNamespace, Path]:
    words = [0x24020001, 0x03E00008, 0] if words is None else words
    root = directory / "project"
    root.mkdir()
    for relative in ("src", "include", "asm", "tools", "build"):
        (root / relative).mkdir()
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
        unit = assemble(generation, "original", program)
        (generation / "objdiff.json").write_text(
            json.dumps({"units": [{"name": name, "target_path": "original.o"} for name in ("alpha", "beta", "gamma")]})
        )
        script = generation / "layout.ld"
        script.write_text(
            f"external = 0x80003000;\nSECTIONS {{ .text 0x{start:X} : {{ *(.text) }} "
            "/DISCARD/ : { *(.reginfo) *(.MIPS.abiflags) *(.pdr) } }\n",
            encoding="utf-8",
        )
        subprocess.run(
            [LINKER, "-T", str(script), "-o", str(generation / "game.elf"), str(unit)], check=True, capture_output=True
        )
        (root / "build" / v).symlink_to(generation.name, target_is_directory=True)
        version_map[v] = Version(v, baserom, hashlib.sha1(baserom.read_bytes()).hexdigest(), split, symbols, ())
        asm = root / "asm" / v / "nonmatchings"
        asm.mkdir(parents=True)
        (asm / "alpha.s").write_text(assembly("alpha", words), encoding="utf-8")
    compiler = Compiler("ido-7.1", "ido", Path("/compiler/cc"), Path(ASSEMBLER), (), root / "tools" / "compiler.sha256")
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
    )
    configured = load_policy()
    policy = SimpleNamespace(
        objdiff_cli=configured.objdiff_cli,
        objdiff_sha256=configured.objdiff_sha256,
        mips_objcopy=configured.mips_objcopy,
        mips_ld=LINKER,
        mips_readelf=READELF,
        mips_objdump=OBJDUMP,
        cpp=cast(Callable[[str], str], tool)("cpp"),
        cppflags=(),
    )
    source = directory / "alpha.c"
    source.write_text("/* NON_MATCHING: returns one. */\nint alpha(void) { return 1; }\n", encoding="utf-8")
    return project, policy, source
