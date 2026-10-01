"""Measure and prove existing C units before porting their version split rows."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from unbake.layout import split, split_apply
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object


@dataclass(frozen=True)
class Candidate:
    function: str
    source: split.Function
    target: split.Function
    identity: str
    differing_words: int
    cause: str = ""


def relocation_masks(path: Path, size: int) -> dict[int, int]:
    """Only fields recorded by the ELF relocation table may be ignored."""
    obj = Object(path)
    section = obj.section(".text")
    if section is None:
        raise Held("port", f"{path}: missing .text")
    masks: dict[int, int] = {}
    for offset, kind, _ in obj.relocations(section):
        if offset >= size:
            continue
        mask = {2: 0xFFFFFFFF, 4: 0x03FFFFFF, 5: 0xFFFF, 6: 0xFFFF, 10: 0xFFFF}.get(kind)
        if mask is None or offset % 4:
            raise Held("port", f"{path}: unsupported relocation {kind} at {offset:#x}")
        masks[offset] = masks.get(offset, 0) | mask
    return masks


def object_path(project: Project, function: split.Function) -> Path:
    return (
        project.build_link(function.version)
        / "obj"
        / ("src" if function.kind == "c" else "asm")
        / (function.path + ".o")
    )


def identity(project: Project, source: split.Function, target: split.Function) -> tuple[str, int, str]:
    left, right = split.words(project, source), split.words(project, target)
    if len(left) != len(right):
        return "different", abs(len(left) - len(right)) // 4, "target size differs"
    if left == right:
        return "identical", 0, ""
    try:
        masks = relocation_masks(object_path(project, source), len(left))
        for offset, mask in relocation_masks(object_path(project, target), len(right)).items():
            masks[offset] = masks.get(offset, 0) | mask
    except (OSError, ValueError, Held) as error:
        return "unknown", -1, str(error)
    differences = sum(
        (int.from_bytes(left[offset : offset + 4], "big") ^ int.from_bytes(right[offset : offset + 4], "big"))
        & (0xFFFFFFFF ^ masks.get(offset, 0))
        != 0
        for offset in range(0, len(left), 4)
    )
    return ("relocations" if differences == 0 else "different"), differences, ""


def functions(project: Project, version: str) -> list[split.Function]:
    """Index symbol aliases once rather than scanning all symbols for every row."""
    configured = project.version(version)
    _, _, segments = split.layout(configured.split)
    _, symbols = split.symbols(configured.symbols)
    aliases: dict[int, list[str]] = {}
    for name, (address, _, _) in symbols.items():
        aliases.setdefault(address, []).append(name)
    result = []
    for segment in segments:
        for index, row in enumerate(segment.rows):
            if row.kind not in ("asm", "c"):
                continue
            address = split.address(row, configured.split)
            stem = Path(row.path).name
            names = aliases.get(address, [])
            name = stem if stem in names or not names else names[0]
            result.append(
                split.Function(
                    version,
                    name,
                    row.start,
                    segment.rows[index + 1].start if index + 1 < len(segment.rows) else int(segment.end or 0),
                    address,
                    row.path,
                    row.kind,
                    tuple(dict.fromkeys((stem, *names))),
                )
            )
    return result


def candidates(project: Project, source_version: str, versions: list[str]) -> list[Candidate]:
    project.version(source_version)
    if not versions or len(set(versions)) != len(versions) or source_version in versions:
        raise Held("port", "target versions must be nonempty, unique and exclude the source version")
    targets: dict[str, dict[str, list[split.Function]]] = {}
    for version in versions:
        by_alias: dict[str, list[split.Function]] = {}
        for target in functions(project, version):
            for alias in target.aliases:
                by_alias.setdefault(alias, []).append(target)
        targets[version] = by_alias
    result = []
    for source in functions(project, source_version):
        if source.kind != "c":
            continue
        for by_alias in targets.values():
            selected = list({row for alias in source.aliases for row in by_alias.get(alias, [])})
            if len(selected) != 1 or selected[0].kind != "asm":
                continue
            target = selected[0]
            relation, differences, cause = identity(project, source, target)
            result.append(Candidate(Path(source.path).name, source, target, relation, differences, cause))
    return sorted(result, key=lambda item: (-(item.target.end - item.target.start), item.function, item.target.version))


def measure(path: Path, rows: list[Candidate]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ("function", "source_version", "version", "source_size", "size", "identity", "differing_words", "cause")
        )
        for row in rows:
            writer.writerow(
                (
                    row.function,
                    row.source.version,
                    row.target.version,
                    row.source.end - row.source.start,
                    row.target.end - row.target.start,
                    row.identity,
                    row.differing_words,
                    row.cause,
                )
            )


def has_branch(project: Project, text: str, version: str) -> bool:
    macros = {macro.split("=", 1)[0] for macro in project.version(version).macros if macro.startswith("VERSION_")}
    for directive in re.finditer(r"^\s*#\s*(?:if|elif)\s+([^\n]+)", text, re.M):
        condition = directive[1]
        for match in re.finditer(r"\bdefined\s*\(\s*(VERSION_\w+)\s*\)", condition):
            if match[1] in macros and not condition[: match.start()].rstrip().endswith("!"):
                return True
    return False


def edits(project: Project, rows: list[Candidate]) -> list[split.Edit]:
    result = []
    for version in dict.fromkeys(row.target.version for row in rows):
        path = project.version(version).split
        before, lines, segments = split.layout(path)
        for candidate in (row for row in rows if row.target.version == version):
            selected = [
                row
                for segment in segments
                for row in segment.rows
                if row.start == candidate.target.start and row.path == candidate.target.path and row.kind == "asm"
            ]
            if len(selected) != 1:
                raise Held("port", f"{candidate.function} VERSION {version}: split row changed")
            row = selected[0]
            lines[row.line] = split.replace_row(lines[row.line], row.match, kind="c", path=candidate.source.path)
        result.append(split.Edit(path, before, "".join(lines), (version,)))
    return result


def required_placements(
    project: Project, candidate: Candidate, compiled: Path, *, include_configured: bool = False
) -> dict[str, int]:
    """Recover missing extern bases from golden operands and their C relocation addends."""
    obj = Object(compiled)
    section = obj.section(".text")
    if section is None:
        raise Held("port", f"{candidate.function}: compiled .text missing")
    _, configured = split.symbols(project.version(candidate.target.version).symbols)
    text = obj.content(section)
    golden = split.words(project, candidate.target)
    relocations = obj.relocations(section)
    pending: dict[str, list[int]] = {}
    result: dict[str, int] = {}
    missing = {
        symbol["name"]
        for _, _, symbol in relocations
        if symbol["section"] == 0 and symbol["name"] and (include_configured or symbol["name"] not in configured)
    }

    def word(data: bytes, offset: int) -> int:
        if offset + 4 > len(data):
            raise Held("port", f"{candidate.function}: relocation outside golden text")
        return int.from_bytes(data[offset : offset + 4], "big")

    def signed(value: int) -> int:
        return value - 0x10000 if value & 0x8000 else value

    def record(name: str, address: int) -> None:
        address &= 0xFFFFFFFF
        if name in result and result[name] != address:
            raise Held("port", f"{candidate.function}: inconsistent golden placement for {name}")
        result[name] = address

    for offset, kind, symbol in relocations:
        name = symbol["name"]
        if name not in missing:
            continue
        source, target = word(text, offset), word(golden, offset)
        if kind == 4:
            destination = ((candidate.target.address + offset + 4) & 0xF0000000) | ((target & 0x03FFFFFF) << 2)
            record(name, destination - ((source & 0x03FFFFFF) << 2))
        elif kind == 5:
            pending.setdefault(name, []).append(offset)
        elif kind == 6:
            highs = pending.pop(name, [])
            if not highs:
                raise Held("port", f"{candidate.function}: unpaired low relocation for {name}")
            for high in highs:
                target_value = ((word(golden, high) & 0xFFFF) << 16) + signed(target & 0xFFFF)
                addend = ((word(text, high) & 0xFFFF) << 16) + signed(source & 0xFFFF)
                record(name, target_value - addend)
        elif kind == 2:
            record(name, target - source)
        else:
            raise Held("port", f"{candidate.function}: missing {name} has unsupported relocation {kind}")
    if pending or missing - result.keys():
        raise Held("port", f"{candidate.function}: missing extern placements could not be proved")
    return result


def port(project: Project, policy: Policy, rows: list[Candidate], scratch: Path, *, apply: bool) -> list[str]:
    from unbake.decomp import trial

    accepted = []
    receipts = []
    placements: dict[str, dict[str, int]] = {}
    for row in rows:
        version = row.target.version
        label = f"{row.function} VERSION {version}"
        source = project.src / (row.source.path + ".c")
        text = source.read_text()
        if row.identity not in ("identical", "relocations") and not has_branch(project, text, version):
            receipts.append(f"HELD(port): {label}: target differs; C has no matching version branch ({row.cause})")
            continue
        try:
            work = scratch / f"{row.function}-{version}"
            result = trial.try_draft(project, policy, source, work, versions=[version])
            if not result.identical_everywhere:
                receipts.append(f"HELD(port): {label}: target-version try is not identical")
                continue
            objects = list(work.rglob(row.function + ".o"))
            if not objects:
                raise Held("port", f"{label}: trial compiled object missing")
            compiled = max(objects, key=lambda path: path.stat().st_mtime_ns)
            required = required_placements(project, row, compiled)
            previous = placements.setdefault(version, {})
            _, configured = split.symbols(project.version(version).symbols)
            for name, address in required.items():
                aliases = [other for other, value in configured.items() if value[0] == address and other != name]
                aliases.extend(other for other, value in previous.items() if value == address and other != name)
                if aliases:
                    raise Held(
                        "port",
                        f"{label}: missing extern {name} shares an address with {aliases[0]}; "
                        "use the existing canonical name in C",
                    )
            conflicts = [name for name, address in required.items() if name in previous and previous[name] != address]
            if conflicts:
                raise Held("port", f"{label}: conflicting placement for {', '.join(conflicts)}")
            previous.update(required)
        except (Held, ValueError, IndexError) as error:
            receipts.append(f"HELD(port): {label}: try failed: {error}")
            continue
        accepted.append(row)
        receipts.append(f"OK(port): {label}: target-version try identical; {row.target.end - row.target.start} bytes")
    planned = edits(project, accepted)
    for version, names in placements.items():
        path = project.version(version).symbols
        before = split.read(path)
        after = before
        for name, address in names.items():
            after += ("" if not after or after.endswith("\n") else "\n") + f"{name} = 0x{address:08X};\n"
        if after != before:
            planned.append(split.Edit(path, before, after, (version,)))
    if apply:
        results = split_apply.apply(project, policy, planned)
        receipts.extend(
            f"{'OK' if result.ok else 'HELD'}(port): {result.version}: {result.sha1_line}" for result in results
        )
        receipts.append(f"OK(port): applied {len(accepted)} proved rows")
    else:
        print(split_apply.diff(planned), end="")
    return receipts
