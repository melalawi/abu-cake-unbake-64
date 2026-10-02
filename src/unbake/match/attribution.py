"""Attribute cartridge failures from linked object extents and symbol evidence."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.layout import split
from unbake.project.config import Project
from unbake.project_tools.elf import Object

_CONTRIBUTION = re.compile(r"^\s+(\.[\w.]+)\s*\n?\s*(0x[\da-fA-F]+)\s+(0x[\da-fA-F]+)\s+(obj/[^\s]+\.o)\s*$", re.M)


def _local(line: str) -> str:
    """Drop directory prefixes so a compiler diagnostic keeps its message, not its paths."""
    return re.sub(r"(?<![\w.])/?(?:[^\s/:]+/)+", "", line.strip())


def material(obj: Object, address: int, size: int) -> bytes | None:
    for index, section in enumerate(obj.sections):
        if section[2] & 2 and section[1] != 8 and section[3] <= address < address + size <= section[3] + section[5]:
            start = address - section[3]
            return obj.content(index)[start : start + size]
    return None


def diagnose(
    project: Project, failures: list[str], generations: dict[str, Path], names: set[str]
) -> dict[str, list[str]]:
    """Compare every allocated input object and defined symbol, retaining all faults."""
    faults: dict[str, list[str]] = {}
    # Placement victims sit at shifted addresses; relocation-only diffs point at
    # shifted or misplaced targets. Both are blamed only when nothing else is.
    placed: dict[str, list[tuple[int, str]]] = {}
    relocated: dict[str, list[str]] = {}
    short: dict[str, list[tuple[int, str]]] = {}

    def blame(name: str, detail: str) -> None:
        if name in names:
            faults.setdefault(name, []).append(detail)

    def shifted(name: str, address: int, detail: str) -> None:
        if name in names:
            placed.setdefault(name, []).append((address, detail))

    def moved(left: bytes, right: bytes) -> bool:
        """Whether produced bytes are the expected bytes displaced by whole words."""
        span = min(len(left), len(right))
        return any(
            span > 2 * delta
            and (left[delta:span] == right[: span - delta] or right[delta:span] == left[: span - delta])
            for delta in range(4, min(span // 2, 1024) + 1, 4)
        )

    def masked(obj: Object, section: int, left: bytes, right: bytes, base: int) -> bool:
        """Whether two section byte runs differ only inside relocated words."""
        words = {offset - base for offset, _, _ in obj.relocations(section)}
        return all(
            left[at : at + 4] == right[at : at + 4] or at in words for at in range(0, min(len(left), len(right)), 4)
        )

    for version in failures:
        if version not in generations:
            continue
        generation = generations[version]
        log = (generation / "build.log").read_text(errors="replace")
        context: list[str] = []
        # A batch compile reports each failed source on one line; the compiler's
        # own diagnostic lines follow it until the next source or driver line.
        compiled: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in log.splitlines():
            failed = re.match(r"^(?:\S*/)?src/([A-Za-z_]\w*)\.c: (.*)$", line)
            if failed:
                current = compiled.setdefault(failed[1], [_local(failed[2])])
                continue
            if current is not None and not re.match(r"^(make|python3|HELD|OK|\S+\.py)\b", line):
                if re.search(r"(?i)\berror\b|:\d+:", line):
                    current.append(_local(line))
                continue
            current = None
            if "in function" in line:
                context = re.findall(r"obj/src/([^\s/:()]+)\.o", line)
            if any(word in line for word in ("Error:", "HELD(compile)", "batch objects failed")):
                for name in re.findall(r"src/([^\s/:()]+)\.c", line):
                    blame(name, f"{version}: compile diagnostic: {line}")
            if "submit.reuse_inputs:" in line:
                for name in re.findall(r"src/([^\s/:()]+)\.c", line):
                    blame(name, f"{version}: {line}")
            if any(
                word in line for word in ("HELD(", "undefined reference", "multiple definition", "overlap", "overflow")
            ):
                for name in context + re.findall(r"obj/src/([^\s/:()]+)\.o", line):
                    blame(name, f"{version}: link diagnostic: {line}")
                context = []
        for name, lines in compiled.items():
            blame(name, f"{version}: compile diagnostic: {'; '.join(lines)[:600]}")
        elf_path = generation / f"{project.name}.elf"
        map_path = generation / f"{project.name}.map"
        if not elf_path.is_file() or not map_path.is_file():
            continue
        elf = Object(elf_path)
        _, _, segments = split.layout(project.version(version).split)
        image = project.version(version).baserom.read_bytes()

        def resident(
            address: int, size: int, spans: list[split.Segment] = segments, rom: bytes = image
        ) -> bytes | None:
            for segment in spans:
                if "vram" in segment.fields and "start" in segment.fields and segment.end is not None:
                    start = int(segment.fields["start"], 0)
                    offset = start + address - int(segment.fields["vram"], 0)
                    if start <= offset < offset + size <= segment.end:
                        return rom[offset : offset + size]
            return None

        owners = {Path(row.path).name: row for row in split.functions(project, version)}
        linked = {symbol["name"]: symbol for table in elf.symbols.values() for symbol in table}
        _, known = split.symbols(project.version(version).symbols)
        oversized = set()
        contributions = _CONTRIBUTION.findall(map_path.read_text())
        for section, _, size_hex, path in contributions:
            name = Path(path).stem
            if path.startswith("obj/src/") and section == ".text" and name in owners:
                row = owners[name]
                if int(size_hex, 16) > row.end - row.start:
                    oversized.add(name)
                    blame(
                        name,
                        f"{version}: object {path}: .text size {int(size_hex, 16)} exceeds target span "
                        f"{row.end - row.start}",
                    )
                elif int(size_hex, 16) < row.end - row.start and name in names:
                    # Alignment may legitimately absorb a short text; it is a cause only before a shift.
                    short.setdefault(name, []).append(
                        (
                            row.address,
                            f"{version}: object {path}: .text size {int(size_hex, 16)} is short of target span "
                            f"{row.end - row.start}",
                        )
                    )
        for function in sorted(names):
            path = generation / "obj/src" / (function + ".o")
            if not path.is_file():
                continue
            obj = Object(path)
            for table in obj.symbols.values():
                for symbol in table:
                    if symbol["section"] == 0 or symbol["info"] >> 4 == 0:
                        continue
                    name = symbol["name"]
                    if (
                        name in known
                        and name in linked
                        and linked[name]["value"] != known[name][0]
                        and (not oversized or function in oversized)
                    ):
                        shifted(
                            function,
                            known[name][0],
                            f"{version}: defined symbol {name}: address "
                            f"0x{linked[name]['value']:08X}, target 0x{known[name][0]:08X}",
                        )
        for section, address_hex, size_hex, path in contributions:
            if not path.startswith("obj/src/") or Path(path).stem not in names:
                continue
            address, size = int(address_hex, 16), int(size_hex, 16)
            if not size or not address:
                continue
            actual, expected = material(elf, address, size), resident(address, size)
            if actual is None or expected is None:
                continue
            if path.startswith("obj/src/") and (not oversized or Path(path).stem in oversized):
                obj = Object(generation / path)
                section_index = obj.section(section)
                for table in obj.symbols.values():
                    for symbol in table:
                        if symbol["section"] != section_index or not symbol["size"]:
                            continue
                        at, count = symbol["value"], symbol["size"]
                        left, right = expected[at : at + count], actual[at : at + count]
                        if left != right:
                            detail = (
                                f"{version}: symbol {symbol['name']} in {path}: "
                                f"expected {left[:16].hex()}, produced {right[:16].hex()}"
                            )
                            if moved(left, right):
                                shifted(Path(path).stem, address + at, detail)
                            elif masked(obj, section_index, left, right, at):
                                if Path(path).stem in names:
                                    relocated.setdefault(Path(path).stem, []).append(detail)
                            else:
                                blame(Path(path).stem, detail)
            if actual != expected:
                if oversized and Path(path).stem not in oversized:
                    continue
                at = next(i for i, (left, right) in enumerate(zip(expected, actual, strict=True)) if left != right)
                name = Path(path).stem
                if path.startswith("obj/src/"):
                    detail = (
                        f"{version}: object {path} {section}+0x{at:X} at 0x{address + at:08X}: "
                        f"expected {expected[at : at + 16].hex()}, produced {actual[at : at + 16].hex()}"
                    )
                    obj = Object(generation / path)
                    index = obj.section(section)
                    if moved(expected, actual):
                        shifted(name, address, detail)
                    elif index is not None and masked(obj, index, expected, actual, 0):
                        if name in names:
                            relocated.setdefault(name, []).append(detail)
                    else:
                        blame(name, detail)
        symbols = [symbol for table in elf.symbols.values() for symbol in table]
        for row in split.functions(project, version):
            name = Path(row.path).name
            if name not in names or (oversized and name not in oversized):
                continue
            for symbol in symbols:
                if symbol["name"] != name or symbol["section"] in (0, 0xFFF1):
                    continue
                if symbol["value"] != row.address:
                    shifted(
                        name,
                        row.address,
                        f"{version}: symbol {name}: address 0x{symbol['value']:08X}, expected 0x{row.address:08X}",
                    )
                    continue
                actual = material(elf, symbol["value"], row.end - row.start)
                expected = image[row.start : row.end]
                if actual is not None and actual != expected and name not in relocated:
                    blame(name, f"{version}: symbol {name}: bytes differ over {len(expected)} target bytes")
    if faults:
        return faults
    if placed:
        # The earliest shifted item follows the cause; later ones move with it.
        first = min(address for items in placed.values() for address, _ in items)
        causes = {name: items for name, items in short.items() if min(a for a, _ in items) < first}
        if causes:
            name = max(causes, key=lambda item: min(a for a, _ in causes[item]))
            return {name: [detail for _, detail in causes[name]]}
        name = min(placed, key=lambda item: min(address for address, _ in placed[item]))
        return {name: [detail for _, detail in placed[name]]}
    return relocated
