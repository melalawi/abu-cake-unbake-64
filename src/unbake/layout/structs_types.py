"""C declarator records and scalar widths for the target ABI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias


@dataclass
class Aggregate:
    kind: str
    name: str
    members: list[Member] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    start: int = 0
    end: int = 0
    body_start: int = 0
    body_end: int = 0
    complete: bool = False


@dataclass(frozen=True)
class Member:
    name: str
    base: str | Aggregate
    operations: tuple[Operation, ...]
    start: int
    end: int
    declaration: str = ""
    bits: int | None = None


Operation: TypeAlias = tuple[str, int | str | None]

# The target ABI fixes these widths independently of the host running the parser.
SCALARS = {
    "char": (1, 1),
    "signed char": (1, 1),
    "unsigned char": (1, 1),
    "short": (2, 2),
    "short int": (2, 2),
    "signed short": (2, 2),
    "unsigned short": (2, 2),
    "unsigned short int": (2, 2),
    "int": (4, 4),
    "signed": (4, 4),
    "signed int": (4, 4),
    "unsigned": (4, 4),
    "unsigned int": (4, 4),
    "long": (4, 4),
    "long int": (4, 4),
    "unsigned long": (4, 4),
    "unsigned long int": (4, 4),
    "long long": (8, 8),
    "long long int": (8, 8),
    "unsigned long long": (8, 8),
    "float": (4, 4),
    "double": (8, 8),
    **{f"{sign}{bits}": (bits // 8, bits // 8) for sign in ("s", "u") for bits in (8, 16, 32, 64)},
    "f32": (4, 4),
    "f64": (8, 8),
}

QUALIFIERS = {
    "const",
    "volatile",
    "restrict",
    "__restrict",
    "signed",
    "unsigned",
    "short",
    "long",
    "int",
    "char",
    "float",
    "double",
    "void",
}
