"""Coalesce inferred declarations into native Splat symbol-file edits."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.needs import LabelNeed, Need, SymbolNeed, register_resolver
from unbake.decomp.rom import NAME
from unbake.decomp.symbols import required, symbol_line
from unbake.layout.split import Edit
from unbake.config import Held, Host, Project

_LINE = re.compile(rf"^\s*({NAME})\s*=\s*(0[xX][\da-fA-F]+|\d+)\s*;\s*(?://(.*))?$")


def resolve(needs: list[Need], project: Project, policy: Host) -> list[Edit]:
    """Coalesce symbol and label needs into reviewable edits without writing files."""
    # Host is required by the resolver protocol; symbol edits use no process tools.
    required(policy, "policy")
    names_from = required(getattr(project, "names_from", None), "project.names_from")
    groups: dict[Path, list[SymbolNeed | LabelNeed]] = {}
    for need in needs:
        if not isinstance(need, (SymbolNeed, LabelNeed)):
            raise Held("symbols", f"needs: unsupported {type(need).__name__}")
        version = project.version(need.version)
        path = Path(required(version.symbols, f"version.{need.version}.symbols"))
        groups.setdefault(path, []).append(need)
    edits = []
    for path, group in groups.items():
        try:
            before = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise Held("symbols", f"{path}: {error}") from error
        lines = before.splitlines()
        existing: dict[str, tuple[int, int, str]] = {}
        for index, line in enumerate(lines):
            if not line.strip() or line.lstrip().startswith(("//", "#")):
                continue
            match = _LINE.fullmatch(line)
            if not match:
                raise Held("symbols", f"{path}:{index + 1}: symbol line required")
            name, value, attrs = match.groups()
            address = int(value, 16 if value.lower().startswith("0x") else 10)
            if name in existing:
                raise Held("symbols", f"{name}: duplicate symbol declaration")
            existing[name] = (address, index, attrs or "")
        selected: dict[str, SymbolNeed] = {}
        for need in group:
            if not isinstance(need, SymbolNeed):
                continue
            symbol_line(need)
            if need.name in selected:
                other = selected[need.name]
                if other.address == need.address:
                    if need.type == "address":
                        continue
                    if other.type == "address":
                        selected[need.name] = need
                        continue
                if (other.address, other.type, other.size) != (need.address, need.type, need.size):
                    raise Held("symbols", f"{need.name}: two-addresses or conflicting type/size")
            selected[need.name] = need
        for need in group:
            if isinstance(need, LabelNeed) and need.name not in selected and need.name not in existing:
                raise Held("symbols", f"{need.name}.type/size: LabelNeed requires SymbolNeed")
            if isinstance(need, LabelNeed):
                address = selected[need.name].address if need.name in selected else existing[need.name][0]
                if address != need.address:
                    raise Held("symbols", f"{need.name}: two-addresses for label")
        for name, need in sorted(selected.items(), key=lambda item: (item[1].address, item[0])):
            named = re.fullmatch(r"D_([\da-fA-F]{8})", name)
            if named and need.version == names_from and int(named[1], 16) != need.address:
                raise Held("symbols", f"{name}: address-named symbol disagrees with 0x{need.address:08X}")
            rendered = symbol_line(need)
            if name in existing:
                address, index, attrs = existing[name]
                if address != need.address:
                    raise Held("symbols", f"{name}: placed-elsewhere at 0x{address:08X}")
                if need.type == "address":
                    continue
                for field, value in (("type", need.type), ("size", need.size)):
                    found = re.search(rf"\b{field}:([^\s]+)", attrs)
                    if found:
                        try:
                            actual = int(found[1], 0) if field == "size" else found[1]
                        except ValueError as error:
                            raise Held("symbols", f"{name}.{field}: invalid existing value") from error
                        if actual != value:
                            raise Held("symbols", f"{name}.{field}: conflicting existing value")
                    else:
                        suffix = value if field == "type" else f"0x{value:X}"
                        if "//" not in lines[index]:
                            lines[index] += " //"
                        lines[index] += f" {field}:{suffix}"
            else:
                if need.type == "address" and any(address == need.address for address, _, _ in existing.values()):
                    rendered += " // absolute:True"
                lines.append(rendered)
        after = "\n".join(lines) + ("\n" if lines else "")
        if after != before:
            edits.append(Edit(path, before, after, tuple(dict.fromkeys(need.version for need in group))))
    return edits


register_resolver(SymbolNeed, 10, resolve)
register_resolver(LabelNeed, 20, resolve)


def data_symbol(
    project: Project, policy: Host, version: str, name: str, address: int, rename_from: str | None
) -> list[Edit]:
    """Add or rename one validated address-only data declaration."""
    from unbake.layout import split

    name = split.name(name)
    if name.startswith("func_") or not 0x80000000 <= address <= 0xFFFFFFFF:
        raise Held("symbols", "data symbol: required data name and runtime address")
    path = project.version(version).symbols
    before, rows = split.symbols(path)
    if any(
        name in function.aliases or function.address <= address < function.address + function.end - function.start
        for function in split.functions(project, version)
    ):
        raise Held("symbols", f"{name}: data symbol overlaps text")
    if rename_from is not None:
        rename_from = split.name(rename_from)
        if rename_from not in rows or rows[rename_from][0] != address:
            raise Held("symbols", f"{rename_from}: rename requires existing symbol at 0x{address:08X}")
        if name in rows and name != rename_from:
            raise Held("symbols", f"{name}: rename target already exists")
        _, index, match = rows[rename_from]
        lines = before.splitlines(keepends=True)
        line = lines[index]
        lines[index] = line[: match.start("name")] + name + line[match.end("name") :]
        after = "".join(lines)
        return [Edit(path, before, after, (version,))] if after != before else []
    need = SymbolNeed(version, name, address, 0, "absolute", "address", 0, "data symbol placement")
    return resolve([need], project, policy)
