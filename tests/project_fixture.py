"""A small ready project on disk, loaded through config.load, and a host from tests.kit.host_values.

Three functions (alpha, beta, gamma) per version, each `li v0, N; jr ra; nop`, one layout group per function.
Callers patch the external tool boundaries they touch (tests.kit.boundary); nothing here starts a process.
"""

import hashlib
import struct
from pathlib import Path

from tests.kit import TempCase, host_values
from unbake import config
from unbake.config import Host, Project
from unbake.layout import map as ownership

FUNCTIONS = ("alpha", "beta", "gamma")


def assembly(function: str, words: list[int]) -> str:
    return (
        f".set noreorder\n.text\n.balign 4\n.globl {function}\n.type {function}, @function\n{function}:\n"
        + "".join(f".word 0x{word:08X}\n" for word in words)
        + f".size {function}, .-{function}\n"
    )


def make(directory: Path, words: list[int] | None = None, versions: tuple[str, ...] = ("us",)) -> tuple[Project, Host]:
    words = [0x24020001, 0x03E00008, 0] if words is None else words
    bodies = {"alpha": words, "beta": [0x24020002, 0x03E00008, 0], "gamma": [0x24020003, 0x03E00008, 0]}
    root = directory / "project"
    for relative in ("src", "include", "roms", "tools/ido-7.1", "build"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "include" / "types.h").write_text("typedef int s32;\n")
    for name in ("cc", "as"):
        (root / "tools/ido-7.1" / name).write_bytes(b"fixture " + name.encode())
    (root / "tools/compilers.sha256").write_text("")
    version_rows = []
    for v in versions:
        start = 0x80001000 if v == versions[0] else 0x80202000
        definitions = root / "versions" / v
        definitions.mkdir(parents=True)
        offsets, cursor = {}, 0x40
        for name in FUNCTIONS:
            offsets[name] = cursor
            cursor += len(bodies[name]) * 4
        (definitions / "game.yaml").write_text(
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            f"  - name: main\n    type: code\n    start: 0x40\n    vram: 0x{start:X}\n    subalign: 4\n    subsegments:\n"
            + "".join(f"      - [0x{offsets[name]:X}, asm, {name}]\n" for name in FUNCTIONS)
            + f"  - [0x{cursor:X}]\n"
        )
        (definitions / "symbol_addrs.txt").write_text(
            "".join(f"{name} = 0x{start + offsets[name] - 0x40:X};\n" for name in FUNCTIONS)
        )
        rom = root / "roms" / f"baserom.{v}.z64"
        all_words = [word for name in FUNCTIONS for word in bodies[name]]
        rom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + struct.pack(f">{len(all_words)}I", *all_words))
        asm = root / "asm" / v
        asm.mkdir(parents=True)
        for name in FUNCTIONS:
            (asm / f"{name}.s").write_text(assembly(name, bodies[name]))
        version_rows.append(
            f'[version.{v}]\nbaserom = "roms/{rom.name}"\n'
            f'baserom_sha1 = "{hashlib.sha1(rom.read_bytes()).hexdigest()}"\n'
            f'split = "versions/{v}/game.yaml"\nsymbols = "versions/{v}/symbol_addrs.txt"\n'
            f'macros = ["VERSION_{v.upper().replace("-", "_")}"]\n\n'
        )
    (root / "config.toml").write_text(
        "schema = 1\n\n[project]\nlayout_cap = 2\n"
        'id = "00000000-0000-4000-8000-000000000001"\nstate = "ready"\nname = "fixture"\ntitle = "Fixture"\n'
        f'names_from = "{versions[0]}"\nversions = {list(versions)!r}\ndefault_compiler = "ido-7.1"\n\n'
        + "".join(version_rows)
        + '[compilers."ido-7.1"]\ncflags = ["-O2", "-G0", "-mips2"]\n\n[units]\n\n'
        '[build]\nasflags = ["-EB", "-mips2", "-G0"]\ncppflags = []\nsn64_asflags = []\n'
    )
    (root / "layout.toml").write_bytes(
        ownership.encoded(ownership.Map(2, tuple(ownership.Group(n, "main", "default", (n,)) for n in FUNCTIONS)))
    )
    host = Host.from_values(host_values(directory), "draft")
    return config.load(root), host


class ProjectCase(TempCase):
    """self.project and self.host for a fresh fixture project (two versions: us, eu)."""

    versions: tuple[str, ...] = ("us", "eu")

    def setUp(self) -> None:
        super().setUp()
        self.project, self.host = make(self.root, versions=self.versions)
