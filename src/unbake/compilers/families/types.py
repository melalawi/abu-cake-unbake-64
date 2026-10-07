"""Compiler-neutral diagnostic and source-location records."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Pseudo:
    number: int
    hard: int | None
    references: int | None
    live_length: int | None
    live_range: tuple[int, int] | None
    priority: int | float | None
    rank: int | None
    conflicts: tuple[int, ...]
    allocator: str
    source_lines: tuple[int, ...]
    name: str | None
    rejections: tuple[tuple[int, str], ...]
    words: int = 1


@dataclass(frozen=True)
class RegisterDifference:
    target_offset: int
    draft_offset: int
    target_hard: int
    draft_hard: int
    candidates: tuple[int, ...]
    holders: tuple[int, ...]
    ambiguous: bool


@dataclass(frozen=True)
class Allocation:
    pseudos: tuple[Pseudo, ...]
    differences: tuple[RegisterDifference, ...]
    limitations: tuple[str, ...]
    hard_registers: tuple[int, ...]
    family: str = ""


@dataclass(frozen=True)
class Instruction:
    uid: int
    kind: str
    line: int
    rtl: str


@dataclass(frozen=True)
class Schedule:
    family: str
    available: bool
    sched2: tuple[Instruction, ...]
    dbr: tuple[Instruction, ...]
    delay_slots: tuple[tuple[int, ...], ...]
    reason: str | None


@dataclass(frozen=True)
class Location:
    file: str
    line: int
    column: int


@dataclass(frozen=True)
class View:
    text: str
    # One expanded token per line; no arithmetic involving header line counts.
    locations: tuple[Location, ...]
    origins: tuple[int | None, ...]


@dataclass(frozen=True)
class RuntimeHelper:
    name: str
    size: int
    arguments: tuple[tuple[str, str], ...]
    result: tuple[str, str]
    proof: str


@dataclass(frozen=True)
class PublicHeader:
    name: str
    selector: str
    aliases: tuple[tuple[str, str, str], ...]
    body: str
