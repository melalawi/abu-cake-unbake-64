"""search-variants: search source variants of a draft for a closer match; keep the best as FUNC.best.c."""

from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.work import compare


@dataclass(frozen=True)
class Searched:
    function: str
    best_file: Path
    best_percent: float
    exact: bool
    steps: Path
    # Mutations the methods proposed and measured (the starting source is not one).
    mutations: int

    def document(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "best_file": str(self.best_file),
            "best_percent": round(self.best_percent, 6),
            "exact": self.exact,
            "steps": str(self.steps),
            "mutations": self.mutations,
        }

    def lines(self) -> list[str]:
        state = "EXACT" if self.exact else f"best {self.best_percent:.2f}%"
        return [f"{self.function}: {state}: {self.best_file}", f"steps: {self.steps}"]


def target_object(function: str, code: bytes) -> bytes:
    """Wrap a cartridge function in a big-endian MIPS ELF32 relocatable object."""
    if not isinstance(function, str) or not function or "\x00" in function:
        raise Held("search", "target_object.function is missing or invalid")
    if not code or len(code) % 4:
        raise Held("search", f"target_object.code for {function} must contain whole MIPS words")
    strings = b"\x00" + function.encode("utf-8") + b"\x00"
    section_names = b"\x00.text\x00.symtab\x00.strtab\x00.shstrtab\x00"
    symbols = bytes(16) + struct.pack(">IIIBBH", 0, 0, 0, 3, 0, 1) + struct.pack(">IIIBBH", 1, 0, len(code), 18, 0, 1)
    content = bytearray(bytes(52))
    headers = [bytes(40)]
    for name, kind, flags, data, link, info, alignment, entry_size in (
        (".text", 1, 6, code, 0, 0, 4, 0),
        (".symtab", 2, 0, symbols, 3, 2, 4, 16),
        (".strtab", 3, 0, strings, 0, 0, 1, 0),
        (".shstrtab", 3, 0, section_names, 0, 0, 1, 0),
    ):
        content.extend(bytes(-len(content) % alignment))
        offset = len(content)
        content.extend(data)
        headers.append(
            struct.pack(
                ">IIIIIIIIII",
                section_names.index(name.encode() + b"\x00"),
                kind,
                flags,
                0,
                offset,
                len(data),
                link,
                info,
                alignment,
                entry_size,
            )
        )
    content.extend(bytes(-len(content) % 4))
    section_offset = len(content)
    content.extend(b"".join(headers))
    content[:52] = (
        b"\x7fELF\x01\x02\x01"
        + bytes(9)
        + struct.pack(">HHIIIIIHHHHHH", 1, 8, 1, 0, 0, section_offset, 536875009, 52, 0, 0, 40, 5, 4)
    )
    return bytes(content)


def search(project: Project, host: Host, file: Path, method: str, seconds: int) -> Searched:
    from unbake import search as methods
    from unbake.search.core import run
    from unbake.search.permute import Permuter

    function = compare.function_of(file)
    out = project.work / function / ".search"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if method == "permute":
        versions = split.holding_versions(project, function)
        version = project.names_from if project.names_from in versions else versions[0]
        row = compare.row_of(project, function, version)
        target = out / f"target-{version}.o"
        atomic_files.write(target, target_object(function, split.words(project, row)))
        generators: list[Any] = [Permuter(version, target, float(seconds))]
    else:
        generators = methods.methods(method)
    # A draft under build/work/FUNC sees its own private headers first, as compare does.
    result = run(compare.view_for(project, file, function), host, file, generators, out, float(seconds))
    best = file.with_name(f"{function}.best.c")
    atomic_files.copyfile(result.source, best)
    return Searched(function, best, result.fuzzy, result.trial.exact, result.steps, result.trials - 1)
