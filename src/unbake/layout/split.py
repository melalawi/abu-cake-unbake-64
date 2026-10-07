"""Plain YAML rows, symbol readers, and measured function boundaries."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from unbake import cache as retention
from unbake.config import Held

if TYPE_CHECKING:
    from unbake.config import Project


class AsmProject(Protocol):
    @property
    def asm(self) -> Path: ...


@dataclass(frozen=True)
class ExtractedText:
    functions: list[Function]
    data: tuple[tuple[int, int], ...]


def _text_data(text: str) -> tuple[tuple[int, int], ...]:
    """Explicit disassembler data evidence, independent of decodable ROM words."""
    labels = list(re.finditer(r"^(glabel|dlabel)\s+([A-Za-z_]\w*)", text, re.M))
    data = []
    for index, label in enumerate(labels):
        stop = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        for word in re.finditer(
            r"/\*\s*([\da-fA-F]+)\s+[\da-fA-F]{8}\s+[\da-fA-F]{8}\s*\*/\s*([^\n]+)",
            text[label.end() : stop],
        ):
            if label[1] == "dlabel" or word[2].lstrip().startswith("."):
                offset = int(word[1], 16)
                data.append((offset, offset + 4))
    return tuple(sorted(set(data)))


def extracted_text(project: AsmProject, version: str, paths: Sequence[Path] | None = None) -> ExtractedText:
    """Read the function boundaries emitted by Splat's disassembler."""
    root = project.asm / version
    functions = []
    data: list[tuple[int, int]] = []
    for path in sorted(root.rglob("*.s") if paths is None else paths):
        text = read(path)
        if not re.search(r"\.section\s+\.text|^\.text\b", text, re.M):
            continue
        data.extend(_text_data(text))
        labels = list(re.finditer(r"^(glabel|dlabel)\s+([A-Za-z_]\w*)", text, re.M))
        for index, label in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
            words = re.findall(
                r"/\*\s*([\da-fA-F]+)\s+([\da-fA-F]{8})\s+[\da-fA-F]{8}\s*\*/\s*([^\n]+)",
                text[label.end() : end],
            )
            if not words:
                continue
            start = int(words[0][0], 16)
            stop = int(words[-1][0], 16) + 4
            address = int(words[0][1], 16)
            if label[1] == "dlabel" or all(emitted.lstrip().startswith(".") for _, _, emitted in words):
                continue
            functions.append(
                Function(
                    version,
                    label[2],
                    start,
                    stop,
                    address,
                    path.relative_to(root).with_suffix("").as_posix(),
                    "asm",
                    (),
                )
            )
    functions.sort(key=lambda function: function.start)
    if not functions and not data:
        raise Held("init", f"VERSION {version}: splat emitted no function boundaries in {root}")
    if any(left.end > right.start for left, right in pairwise(functions)):
        raise Held("init", f"VERSION {version}: overlapping splat function boundaries")
    return ExtractedText(functions, tuple(sorted(set(data))))


@dataclass(frozen=True)
class Edit:
    path: Path
    before: str
    after: str
    versions: tuple[str, ...]


@dataclass(frozen=True)
class Function:
    version: str
    name: str
    start: int
    end: int
    address: int
    path: str
    kind: str
    aliases: tuple[str, ...]
    entries: tuple[tuple[str, int], ...] = ()


@dataclass
class Segment:
    fields: dict[str, str] = field(default_factory=dict)
    rows: list[Row] = field(default_factory=list)
    end: int | None = None


@dataclass(eq=False)
class Row:
    """A split row is identified by its place in a parsed layout, never by value."""

    line: int
    start: int
    kind: str
    path: str
    segment: Segment
    match: re.Match[str]


NUMBER = r"(?:0[xX][0-9a-fA-F]+|[0-9]+)"


ROW = re.compile(
    rf"^(?P<indent>\s*)-\s*\[\s*(?P<start>{NUMBER})\s*,\s*"
    r"(?P<kind>[.A-Za-z_][.A-Za-z_0-9]*)\s*,\s*"
    r"(?P<path>[^,\]\r\n]+?)(?P<alignment>\s*,\s*\{\s*align:\s*(?P<align>" + NUMBER + r")\s*\})?"
    r"(?P<tail>[ \t]*\][ \t]*)(?P<newline>\r?\n)?$"
)


SYMBOL = re.compile(rf"^\s*(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<address>{NUMBER})\s*;")


NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Function rows: unmatched asm, published C, and landed original asm (hasm, decomp.original_asm).
CODE_KINDS = ("asm", "c", "hasm")


def name(value: object, label: str = "function") -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise Held("split", f"{label}: required symbol name")
    return value


def number(value: object, label: str) -> int:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        if not isinstance(value, (str, int)):
            raise ValueError
        result = int(value, 0) if isinstance(value, str) else int(value)
        if result < 0 or (not isinstance(value, (str, int))):
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise Held("split", f"{label}: required nonnegative address") from None


def read(path: Path) -> str:
    try:
        return Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise Held("split", f"{path}: {exc}") from exc


def plain(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def layout(path: Path) -> tuple[str, list[str], list[Segment]]:
    from unbake.cache import parsed

    text, lines, segments = parsed("split.layout", path, lambda: parse_layout(path, read(path)))
    return text, list(lines), segments


def parse_layout(path: Path, text: str) -> tuple[str, list[str], list[Segment]]:
    """Parse layout text without publishing a temporary YAML file."""
    lines = text.splitlines(keepends=True)
    segments: list[Segment] = []
    boundaries: list[int] = []
    current = None
    segment_indent = None
    sub_indent = None
    in_segments = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            raise Held("split", f"{path}:{index + 1}: plain YAML required")
        if stripped == "segments:":
            in_segments = True
            continue
        if not in_segments:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if segment_indent is None and stripped.startswith("- "):
            segment_indent = indent
        if indent == segment_indent and stripped.startswith("- "):
            sub_indent = None
            current = None
            if stripped.startswith("- ["):
                match = re.match(rf"-\s*\[\s*({NUMBER})(?:\s*[,\]])", stripped)
                if not match:
                    raise Held("split", f"{path}:{index + 1}: segment start")
                boundaries.append(number(match[1], f"{path}: start"))
            elif stripped.startswith("- {") and stripped.endswith("}"):
                fields = {}
                for item in stripped[3:-1].split(","):
                    match = re.fullmatch(r"\s*(\w+):\s*(.*?)\s*", item)
                    if match is None:
                        raise Held("split", f"{path}:{index + 1}: segment mapping")
                    fields[match[1]] = plain(match[2])
                current = Segment(fields)
                segments.append(current)
            else:
                match = re.match(r"-\s*(\w+):\s*(.*)", stripped)
                if not match:
                    raise Held("split", f"{path}:{index + 1}: segment row")
                current = Segment({match[1]: plain(match[2])})
                segments.append(current)
            continue
        if current is None:
            continue
        if stripped == "subsegments:":
            sub_indent = indent
            continue
        if sub_indent is not None and indent > sub_indent:
            match = ROW.fullmatch(line)
            if not match:
                raise Held("split", f"{path}:{index + 1}: plain subsegment row required")
            row = Row(
                index, number(match["start"], f"{path}: start"), match["kind"], plain(match["path"]), current, match
            )
            current.rows.append(row)
        else:
            sub_indent = None
            match = re.match(r"(\w+):\s*(.*)", stripped)
            if match:
                current.fields[match[1]] = plain(match[2])
    if not in_segments:
        raise Held("split", f"{path}: segments")
    for segment in segments:
        if "start" in segment.fields:
            boundaries.append(number(segment.fields["start"], f"{path}: segment start"))
    for segment in segments:
        if "start" not in segment.fields and not segment.rows:
            continue
        start = number(segment.fields.get("start"), f"{path}: segment start")
        following = [boundary for boundary in boundaries if boundary > start]
        if "end" in segment.fields:
            segment.end = number(segment.fields["end"], f"{path}: segment end")
        elif following:
            segment.end = min(following)
        elif segment.rows:
            raise Held("split", f"{path}: segment end")
        if not segment.rows:
            continue
        assert segment.end is not None
        starts = [row.start for row in segment.rows]
        terminal = segment.rows[-1]
        if (
            starts != sorted(set(starts))
            or starts[0] < start
            or starts[-1] > segment.end
            or (starts[-1] == segment.end and terminal.kind not in ("bss", ".bss"))
        ):
            raise Held("split", f"{path}: subsegment boundaries")
    return text, lines, segments


def symbols(path: Path) -> tuple[str, dict[str, tuple[int, int, re.Match[str]]]]:
    from unbake.cache import parsed

    text, result = parsed("split.symbols", path, lambda: _symbols(path))
    return text, dict(result)


def _symbols(path: Path) -> tuple[str, dict[str, tuple[int, int, re.Match[str]]]]:
    text = read(path)
    result = {}
    for index, line in enumerate(text.splitlines(keepends=True)):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        match = SYMBOL.match(line)
        if not match:
            raise Held("split", f"{path}:{index + 1}: symbol line")
        name = match["name"]
        if name in result:
            raise Held("split", f"{path}: duplicate symbol {name}")
        result[name] = (int(match["address"], 0), index, match)
    return text, result


def address(row: Row, path: Path) -> int:
    segment = row.segment
    start = number(segment.fields.get("start"), f"{path}: segment start")
    vram = number(segment.fields.get("vram"), f"{path}: segment vram")
    return vram + row.start - start


def end(row: Row) -> int:
    rows = row.segment.rows
    index = rows.index(row)
    if index + 1 < len(rows):
        return rows[index + 1].start
    assert row.segment.end is not None
    return row.segment.end


def bss_end(project: Project, version: str, segment: str) -> int:
    """Return the explicit final BSS VRAM boundary for a named segment."""
    path = project.version(version).split
    _, _, segments = layout(path)
    selected = [item for item in segments if item.fields.get("name") == segment]
    if len(selected) != 1:
        raise Held("split", f"{segment}.bss_end: required one named segment")
    item = selected[0]
    if "bss_end" in item.fields:
        return number(item.fields["bss_end"], f"{segment}.bss_end")
    size = number(item.fields.get("bss_size"), f"{segment}.bss_size")
    if item.end is None:
        raise Held("split", f"{segment}.bss_end: missing segment end")
    start = number(item.fields.get("start"), f"{segment}.start")
    vram = number(item.fields.get("vram"), f"{segment}.vram")
    return vram + item.end - start + size


def functions(project: Project, v: str) -> list[Function]:
    """List named function rows with explicit ROM and VRAM boundaries."""
    return list(_rows(project, v))


ASM_ROWS = "split.functions.asm"


def _rows(project: Project, v: str) -> tuple[Function, ...]:
    """functions(), shared read-only: one object while the split, symbols and asm tree are unchanged.

    The asm tree counts by its directories' stat signatures: the tool writes files there by rename, and the
    extract step, which overwrites splat output in place, forgets this memo when it ends."""

    from unbake import inputs
    from unbake.cache import memo, parsed

    version = project.version(v)
    rows = parsed("split.functions", (version.split, version.symbols), lambda: _functions(project, v), extra=v)
    asm = getattr(project, "asm", None)
    root = None if asm is None else asm / v
    tree = (
        tuple(
            (path.relative_to(root).as_posix(), inputs.digest(path, algorithm="sha256", reuse=retention.configured()))
            for path in sorted(root.rglob("*.s"))
        )
        if root is not None and root.is_dir()
        else ()
    )

    def filtered() -> tuple[tuple[Function, ...], object]:
        output = []
        for row in rows:
            path = None if root is None else root / (row.path + ".s")
            # Old layouts can still call explicitly emitted data an asm row. Keep
            # that interval out of every function consumer, without editing YAML.
            if path is not None and row.kind == "asm" and path.is_file():

                def text_data(path: Path = path) -> tuple[tuple[int, int], ...]:
                    return _text_data(read(path))

                data = parsed("split.text_data", path, text_data)
                if any(start < row.end and row.start < stop for start, stop in data):
                    continue
            output.append(row)
        # The immutable parsed rows are the exact semantic inventory.
        return tuple(output), rows

    return memo(ASM_ROWS, (v, tuple(rows), tree), filtered, size=retention.memory_size, copy_out=retention.clone)[0]


def owners_by_alias(project: Project, v: str) -> dict[str, list[Function]]:
    """Function rows keyed by every row stem and symbol alias, shared read-only."""
    from unbake.cache import memo

    rows = _rows(project, v)

    def build() -> tuple[dict[str, list[Function]], tuple[Function, ...]]:
        index: dict[str, list[Function]] = {}
        for row in rows:
            for alias in row.aliases:
                index.setdefault(alias, []).append(row)
        return index, rows

    # Keyed by the immutable row values, including every row on each call.
    return memo("split.aliases", (v, tuple(rows)), build, size=retention.memory_size, copy_out=retention.clone)[0]


def holding_versions(
    project: Project, function: str, owners: Mapping[str, Mapping[str, list[Function]]] | None = None
) -> tuple[str, ...]:
    """Versions whose split has a code row named for function, in project order.

    owners holds owners_by_alias per VERSION when a caller already read it once.
    """
    versions = tuple(
        v
        for v in project.versions
        if any(
            Path(row.path).name == function
            for row in (owners[v] if owners is not None else owners_by_alias(project, v)).get(function, ())
        )
    )
    if not versions:
        raise Held("match", f"{function}: split row missing in every VERSION")
    return versions


def _functions(project: Project, v: str) -> list[Function]:
    version = project.version(v)
    _, _, segments = layout(version.split)
    _, symbol_rows = symbols(version.symbols)
    by_address: dict[int, list[str]] = {}
    for symbol_name, entry in symbol_rows.items():
        by_address.setdefault(entry[0], []).append(symbol_name)
    function_symbols = sorted(
        (entry[0], name) for name, entry in symbol_rows.items() if re.search(r"\btype\s*:\s*func\b", entry[2].string)
    )
    function_addresses = [value for value, _ in function_symbols]
    result = []
    for segment in segments:
        for index, row in enumerate(segment.rows):
            if row.kind not in CODE_KINDS:
                continue
            vram_address = address(row, version.split)
            stem = Path(row.path).name
            aliases = tuple(by_address.get(vram_address, ()))
            name = stem if stem in aliases or not aliases else aliases[0]
            stop = segment.rows[index + 1].start if index + 1 < len(segment.rows) else end(row)
            result.append(
                Function(
                    v,
                    name,
                    row.start,
                    stop,
                    vram_address,
                    row.path,
                    row.kind,
                    tuple(dict.fromkeys((stem, *aliases))),
                    tuple((alias, 0) for alias in aliases)
                    + tuple(
                        (entry_name, entry_address - vram_address)
                        for entry_address, entry_name in function_symbols[
                            bisect_left(function_addresses, vram_address + 1) : bisect_left(
                                function_addresses, vram_address + stop - row.start
                            )
                        ]
                    ),
                )
            )
    return result


def members(project: Project, v: str) -> list[Function]:
    """Every function of VERSION v: the rows of split.functions, with each C row that a merge left holding more
    than one function (inner `type: func` entries) split into one interval per function. The rows stay the
    units the build links; this is the function view the map and the type solve read."""
    result = []
    for row in functions(project, v):
        inner = sorted((offset, name) for name, offset in row.entries if offset > 0)
        if row.kind != "c" or not inner:
            result.append(row)
            continue
        starts = [(0, row.name), *inner]
        for index, (offset, name) in enumerate(starts):
            stop = starts[index + 1][0] if index + 1 < len(starts) else row.end - row.start
            own = (name, *(alias for alias in row.aliases if index == 0 and alias != name))
            result.append(
                Function(
                    v,
                    name,
                    row.start + offset,
                    row.start + stop,
                    row.address + offset,
                    row.path,
                    row.kind,
                    own,
                    tuple((alias, 0) for alias in own),
                )
            )
    return result


def member_owners(project: Project, function: str) -> dict[str, Function]:
    """Current build rows containing a named function, including measured merged entries."""
    result = {}
    for version in project.versions:
        rows = [
            row
            for row in functions(project, version)
            if function in row.aliases or any(name == function for name, _ in row.entries)
        ]
        if len(rows) > 1:
            raise Held("match", f"{function}: ambiguous containing unit in VERSION {version}")
        if rows:
            result[version] = rows[0]
    if not result:
        raise Held("match", f"{function}: function identity missing in every VERSION")
    return result


def replace_row(
    line: str, match: re.Match[str], *, start: str | None = None, kind: str | None = None, path: str | None = None
) -> str:
    replacements = {"start": start, "kind": kind, "path": path}
    for key in ("path", "kind", "start"):
        value = replacements[key]
        if value is None:
            continue
        if key == "path":
            old = match[key].strip()
            if old and old[0] in "'\"" and old[-1] == old[0]:
                value = old[0] + value + old[0]
        line = line[: match.start(key)] + value + line[match.end(key) :]
    return line


def words(project: Project, function: Function) -> bytes:
    path = project.version(function.version).baserom
    try:
        with Path(path).open("rb") as stream:
            stream.seek(function.start)
            words = stream.read(function.end - function.start)
    except OSError as exc:
        raise Held("split", f"{path}: {exc}") from exc
    if not words or len(words) != function.end - function.start or len(words) % 4:
        raise Held("split", f"{path}: function {function.name} word range")
    return words
