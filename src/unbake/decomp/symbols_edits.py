"""Coalesce inferred declarations into native Splat symbol-file edits."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.config import Held, Host, Project
from unbake.decomp.needs import LabelNeed, Need, SymbolNeed, register_resolver
from unbake.decomp.rom import NAME
from unbake.decomp.symbols import required, symbol_line
from unbake.layout.split import Edit
from unbake.process import capture
from unbake.process import named as cause_named

_LINE = re.compile(rf"^\s*({NAME})\s*=\s*(0[xX][\da-fA-F]+|\d+)\s*;\s*(?://(.*))?$")


def resolve(needs: list[Need], project: Project, policy: Host) -> list[Edit]:
    """Coalesce symbol and label needs into reviewable edits without writing files."""
    # Host is required by the resolver protocol; symbol edits use no process tools.
    required(policy, "policy")
    names_from = required(getattr(project, "names_from", None), "project.names_from")
    groups: dict[Path, list[SymbolNeed | LabelNeed]] = {}
    for need in needs:
        if not isinstance(need, (SymbolNeed, LabelNeed)):
            raise Held(
                cause_named(
                    "needs", f"needs: unsupported {type(need).__name__}", owner="decomp.symbols_edits", stage="symbols"
                )
            )
        version = project.version(need.version)
        path = Path(required(version.symbols, f"version.{need.version}.symbols"))
        groups.setdefault(path, []).append(need)
    edits = []
    for path, group in groups.items():
        try:
            before = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(f"{path}", f"{path}: {error}", owner="decomp.symbols_edits", stage="symbols"),
                )
            ) from error
        lines = before.splitlines()
        existing: dict[str, tuple[int, int, str]] = {}
        for index, line in enumerate(lines):
            if not line.strip() or line.lstrip().startswith(("//", "#")):
                continue
            match = _LINE.fullmatch(line)
            if not match:
                raise Held(
                    cause_named(
                        f"{path}",
                        f"{path}:{index + 1}: symbol line required",
                        owner="decomp.symbols_edits",
                        stage="symbols",
                    )
                )
            name, value, attrs = match.groups()
            address = int(value, 16 if value.lower().startswith("0x") else 10)
            if name in existing:
                raise Held(
                    cause_named(
                        f"{name}",
                        f"{name}: duplicate symbol declaration",
                        owner="decomp.symbols_edits",
                        stage="symbols",
                    )
                )
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
                    raise Held(
                        cause_named(
                            f"{need.name}",
                            f"{need.name}: two-addresses or conflicting type/size",
                            owner="decomp.symbols_edits",
                            stage="symbols",
                        )
                    )
            selected[need.name] = need
        for need in group:
            if isinstance(need, LabelNeed) and need.name not in selected and need.name not in existing:
                raise Held(
                    cause_named(
                        f"{need.name}.type/size",
                        f"{need.name}.type/size: LabelNeed requires SymbolNeed",
                        owner="decomp.symbols_edits",
                        stage="symbols",
                    )
                )
            if isinstance(need, LabelNeed):
                address = selected[need.name].address if need.name in selected else existing[need.name][0]
                if address != need.address:
                    raise Held(
                        cause_named(
                            f"{need.name}",
                            f"{need.name}: two-addresses for label",
                            owner="decomp.symbols_edits",
                            stage="symbols",
                        )
                    )
        for name, need in sorted(selected.items(), key=lambda item: (item[1].address, item[0])):
            named = re.fullmatch(r"D_([\da-fA-F]{8})", name)
            if named and need.version == names_from and int(named[1], 16) != need.address:
                raise Held(
                    cause_named(
                        f"{name}",
                        f"{name}: address-named symbol disagrees with 0x{need.address:08X}",
                        owner="decomp.symbols_edits",
                        stage="symbols",
                    )
                )
            rendered = symbol_line(need)
            if name in existing:
                address, index, attrs = existing[name]
                if address != need.address:
                    raise Held(
                        cause_named(
                            f"{name}",
                            f"{name}: placed-elsewhere at 0x{address:08X}",
                            owner="decomp.symbols_edits",
                            stage="symbols",
                        )
                    )
                if need.type == "address":
                    continue
                for field, value in (("type", need.type), ("size", need.size)):
                    found = re.search(rf"\b{field}:([^\s]+)", attrs)
                    if found:
                        try:
                            actual = int(found[1], 0) if field == "size" else found[1]
                        except ValueError as error:
                            raise Held(
                                capture(
                                    error,
                                    cause=cause_named(
                                        f"{name}.{field}",
                                        f"{name}.{field}: invalid existing value",
                                        owner="decomp.symbols_edits",
                                        stage="symbols",
                                    ),
                                )
                            ) from error
                        if actual != value:
                            raise Held(
                                cause_named(
                                    f"{name}.{field}",
                                    f"{name}.{field}: conflicting existing value",
                                    owner="decomp.symbols_edits",
                                    stage="symbols",
                                )
                            )
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
        raise Held(
            cause_named(
                "decomp.symbols_edits.data_symbol",
                "data symbol: required data name and runtime address",
                owner="decomp.symbols_edits",
                stage="symbols",
            )
        )
    path = project.version(version).symbols
    before, rows = split.symbols(path)
    if any(
        name in function.aliases or function.address <= address < function.address + function.end - function.start
        for function in split.functions(project, version)
    ):
        raise Held(
            cause_named(f"{name}", f"{name}: data symbol overlaps text", owner="decomp.symbols_edits", stage="symbols")
        )
    if rename_from is not None:
        rename_from = split.name(rename_from)
        if rename_from not in rows or rows[rename_from][0] != address:
            raise Held(
                cause_named(
                    f"{rename_from}",
                    f"{rename_from}: rename requires existing symbol at 0x{address:08X}",
                    owner="decomp.symbols_edits",
                    stage="symbols",
                )
            )
        if name in rows and name != rename_from:
            raise Held(
                cause_named(
                    f"{name}", f"{name}: rename target already exists", owner="decomp.symbols_edits", stage="symbols"
                )
            )
        _, index, match = rows[rename_from]
        lines = before.splitlines(keepends=True)
        line = lines[index]
        lines[index] = line[: match.start("name")] + name + line[match.end("name") :]
        after = "".join(lines)
        return [Edit(path, before, after, (version,))] if after != before else []
    need = SymbolNeed(version, name, address, 0, "absolute", "address", 0, "data symbol placement")
    return resolve([need], project, policy)
