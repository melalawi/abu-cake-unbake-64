"""Original asm: functions no configured compiler can emit from C (work.shape.original), landed as byte-exact
src/NAME.s and recorded in unbake-original-asm.json as {NAME: {rule}}.

A split row of kind `hasm` is a landed original-asm function; the build assembles src/NAME.s for it. The guard
refuses any src/*.s without a record, any record without its hasm row and source, any rule outside the closed
set (compilers.families.mips.ORIGINAL_RULES), and any record whose rule the row's ROM bytes do not prove against
every configured compiler. Nothing else in the tool writes .s sources.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake import atomic as atomic_files
from unbake import process
from unbake.compilers.families.mips import ORIGINAL_RULES
from unbake.config import Held
from unbake.layout import split
from unbake.work import shape

if TYPE_CHECKING:
    from unbake.config import Host, Project

MANIFEST = "unbake-original-asm.json"
_REGISTER = re.compile(
    r"(?<![\w$.])(zero|at|v[01]|a[0-3]|t[0-9]|s[0-8]|k[01]|gp|sp|fp|ra)(?![\w])",
)
_TARGET = re.compile(r"0x([0-9a-f]{8})$")
_ERROR_LINE = re.compile(r":(\d+): Error:")
_JUMPS = ("j", "jal")


@dataclass(frozen=True)
class Record:
    rule: str


def load(project: Project) -> dict[str, Record]:
    """The records; an absent file is no records."""
    path = project.root / MANIFEST
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Held("original-asm", f"{MANIFEST}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != 1 or set(value) != {"schema", "functions"}:
        raise Held("original-asm", f"{MANIFEST}: expected schema 1 and functions")
    functions = value["functions"]
    if not isinstance(functions, dict):
        raise Held("original-asm", f"{MANIFEST}.functions: expected a table")
    records = {}
    for name, row in functions.items():
        if not split.NAME.fullmatch(name) or not isinstance(row, dict) or set(row) != {"rule"}:
            raise Held("original-asm", f"{MANIFEST}.functions.{name}: expected {{rule}}")
        if row["rule"] not in ORIGINAL_RULES:
            known = ", ".join(sorted(ORIGINAL_RULES))
            raise Held("original-asm", f"{MANIFEST}.functions.{name}.rule: {row['rule']!r} is not one of {known}")
        records[name] = Record(row["rule"])
    return records


def dumps(records: dict[str, Record]) -> str:
    functions = {name: {"rule": record.rule} for name, record in sorted(records.items())}
    return json.dumps({"schema": 1, "functions": functions}, indent=2) + "\n"


def prove(project: Project, row: split.Function, data: bytes) -> shape.Original:
    """The original-asm proof of ROW's bytes against every configured compiler; refused when no rule holds."""
    found = shape.original(shape.words_of(data), shape.emitters(project.compilers.values()))
    if found is None:
        raise Held("original-asm", f"original.not_original: {row.version} {row.name}: no original-asm rule holds")
    return found


def guard(project: Project) -> None:
    """Refuse .s sources and hasm rows the records do not cover, and records the ROM no longer proves."""
    records = load(project)
    sources = {path.stem for path in project.src.glob("*.s")}
    unrecorded = sorted(sources - set(records))
    if unrecorded:
        raise Held("original-asm", f"original.unrecorded: src/{unrecorded[0]}.s has no {MANIFEST} record")
    rows: set[str] = set()
    for version in project.versions:
        for row in split.functions(project, version):
            if row.kind != "hasm":
                continue
            name = Path(row.path).name
            rows.add(name)
            record = records.get(name)
            if record is None:
                raise Held("original-asm", f"original.unrecorded: {version} hasm row {name} has no record")
            if name not in sources:
                raise Held("original-asm", f"original.source: {version} hasm row {name} has no src/{name}.s")
            found = prove(project, row, split.words(project, row))
            if found.rule != record.rule:
                raise Held(
                    "original-asm", f"original.rule: {version} {name}: recorded {record.rule}, ROM proves {found.rule}"
                )
    missing = sorted(set(records) - rows)
    if missing:
        raise Held("original-asm", f"original.row: {missing[0]} is recorded but no VERSION has its hasm row")


def _disassemble(host: Host, data: bytes, address: int, work: Path) -> list[str]:
    """One objdump line (mnemonic and operands) per word of DATA at ADDRESS."""
    blob = work / "body.bin"
    atomic_files.write(blob, data)
    out = process.run_tool(
        [
            str(host.mips_objdump),
            "-D",
            "-z",
            "-b",
            "binary",
            "-m",
            "mips:4300",
            "-EB",
            f"--adjust-vma=0x{address:X}",
            str(blob),
        ],
        work,
        "original-asm",
    )
    lines = {}
    for line in out.splitlines():
        match = re.match(r"\s*([0-9a-f]+):\s+([0-9a-f]{8})\s+(.*)$", line)
        if match:
            lines[int(match[1], 16)] = re.sub(r"\s+", " ", match[3].strip())
    return [lines.get(address + 4 * index, "") for index in range(len(data) // 4)]


def _operand_text(text: str, start: int, end: int) -> tuple[str, int | None]:
    """objdump syntax as GNU as reads it: $-prefixed registers, $31 for the FCSR, a branch target inside the
    body as a local label. Returns the text and the in-body target offset (None when there is none)."""
    mnemonic, _, operands = text.partition(" ")
    operands = _REGISTER.sub(r"$\1", operands).replace("c1_fcsr", "$31")
    target = _TARGET.search(operands)
    offset = None
    if target:
        value = int(target[1], 16)
        if start <= value < end:
            offset = value - start
            operands = operands[: target.start()] + f".L{offset:X}"
    return (f"{mnemonic} {operands}".strip(), offset)


Body = list[tuple[str, str]]


def body(data: bytes, address: int, lines: list[str]) -> tuple[Body, set[int]]:
    """(instruction, comment) per word and the in-body branch target offsets: real mnemonics, local labels for
    branches inside the body, j/jal and anything objdump cannot spell as `.word` with the instruction as a comment
    (no relocation, the same bytes)."""
    result: Body = []
    labels = set()
    end = address + len(data)
    for word, text in zip(shape.words_of(data), lines, strict=True):
        mnemonic = text.split(" ", 1)[0]
        if not text or mnemonic in _JUMPS or text.startswith("."):
            result.append((f".word 0x{word:08X}", text))
            continue
        spelled, offset = _operand_text(text, address, end)
        if offset is not None:
            labels.add(offset)
        elif _TARGET.search(spelled):
            result.append((f".word 0x{word:08X}", text))
            continue
        result.append((spelled, ""))
    return result, labels


def render(name: str, found: shape.Original, instructions: Body, labels: set[int]) -> tuple[str, dict[int, int]]:
    """The .s text and, for each 1-based text line holding an instruction, that instruction's index."""
    out = [
        f"# {name}: original asm ({found.rule}: {found.evidence}).",
        f"# Written by unbake from the ROM and recorded in {MANIFEST}.",
        ".set noreorder",
        ".set noat",
        ".text",
        f".globl {name}",
        f"{name}:",
    ]
    positions = {}
    for index, (instruction, comment) in enumerate(instructions):
        if index * 4 in labels:
            out.append(f".L{index * 4:X}:")
        out.append(f"    {instruction}" + (f"  # {comment}" if comment else ""))
        positions[len(out)] = index
    return "\n".join(out) + "\n", positions


def assemble(project: Project, host: Host, text: str, row: split.Function, work: Path) -> bytes:
    """Assemble TEXT with the build's flags and link it alone at ROW's address; return the .text bytes."""
    from unbake import runner

    path = work / f"{Path(row.path).name}.s"
    atomic_files.text(path, text)
    obj = work / "unit.o"
    process.run_tool([str(host.mips_as), *project.asflags, "-o", str(obj), str(path)], work, "original-asm")
    return runner.link(project, host, obj, row.version, row, work)


def write_source(project: Project, host: Host, row: split.Function, data: bytes, found: shape.Original) -> str:
    """The byte-exact .s text for ROW: objdump spelling, with every line GNU as rejects or encodes otherwise
    replaced by its `.word` (at most three passes); refused if the result still differs from the ROM."""
    words = shape.words_of(data)
    name = Path(row.path).name
    with tempfile.TemporaryDirectory(prefix="original-") as temporary:
        work = Path(temporary)
        instructions, labels = body(data, row.address, _disassemble(host, data, row.address, work))
        for _ in range(3):
            text, positions = render(name, found, instructions, labels)
            try:
                built = assemble(project, host, text, row, work)
            except Held as error:
                numbers = [int(number) for number in _ERROR_LINE.findall(error.reason)]
                if not numbers or any(number not in positions for number in numbers):
                    raise
                bad = [positions[number] for number in numbers]
            else:
                if built == data:
                    return text
                if len(built) != len(data):
                    break
                pairs = zip(shape.words_of(built), words, strict=True)
                bad = [index for index, (have, want) in enumerate(pairs) if have != want]
            for index in bad:
                instructions[index] = (f".word 0x{words[index]:08X}", instructions[index][0])
    raise Held("original-asm", f"original.mismatch: {row.version} {name}: the .s does not assemble to the ROM row")
