"""Small explicit ELF32 tables for parser fixtures; no assembler or linker."""

import struct
from pathlib import Path


def write_object(path: Path, sections: dict[str, bytes], symbols=(), *, relocations=(), addresses=None, linked=False):
    symbols = sorted(symbols, key=lambda row: (row[4] if len(row) > 4 else 0x12) >> 4)
    original_sections = list(sections)
    for section in dict.fromkeys(row[0] for row in relocations):
        sections = {
            **sections,
            ".rel" + section: b"".join(
                struct.pack(">II", offset, (next(i for i, row in enumerate(symbols, 1) if row[0] == name) << 8) | kind)
                for sec, offset, kind, name in relocations
                if sec == section
            ),
        }
    names = b"\0" + b"\0".join(name.encode() for name in [*sections, ".strtab", ".symtab", ".shstrtab"]) + b"\0"
    strings = b"\0" + b"\0".join(name.encode() for name, *_ in symbols) + b"\0"
    entries = bytes(16)
    for row in symbols:
        name, section, value, size = row[:4]
        info = row[4] if len(row) > 4 else 0x12
        entries += struct.pack(
            ">IIIBBH",
            strings.index(name.encode() + b"\0"),
            value,
            size,
            info,
            0,
            0xFFF1 if section == "ABS" else list(sections).index(section) + 1 if section else 0,
        )
    contents = {**sections, ".strtab": strings, ".symtab": entries, ".shstrtab": names}
    data = bytearray(52)
    rows = [bytes(40)]
    for _index, (name, content) in enumerate(contents.items(), 1):
        data.extend(bytes(-len(data) % 4))
        at = len(data)
        data.extend(content)
        kind = (
            2 if name == ".symtab" else 3 if name in (".strtab", ".shstrtab") else 9 if name.startswith(".rel.") else 1
        )
        rows.append(
            struct.pack(
                ">10I",
                names.index(name.encode() + b"\0"),
                kind,
                6 if name in (".text", ".overlay") else 2 if name in original_sections else 0,
                (addresses or {}).get(name, 0),
                at,
                len(content),
                len(sections) + 1 if kind == 2 else len(sections) + 2 if kind == 9 else 0,
                1 + sum((row[4] if len(row) > 4 else 0x12) >> 4 == 0 for row in symbols)
                if kind == 2
                else list(sections).index(name[4:]) + 1
                if kind == 9
                else 0,
                4,
                16 if kind == 2 else 8 if kind == 9 else 0,
            )
        )
    data.extend(bytes(-len(data) % 4))
    table = len(data)
    data.extend(b"".join(rows))
    data[:52] = (
        b"\x7fELF\x01\x02\x01"
        + bytes(9)
        + struct.pack(
            ">HHIIIIIHHHHHH", 2 if linked else 1, 8, 1, 0, 0, table, 0, 52, 0, 0, 40, len(rows), len(rows) - 1
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def linked_fixture(path, objects, addresses):
    """Write an explicit linked-image fixture from the tiny relocatable tables."""
    from unbake.project_tools.elf import Object

    contents, symbols = {}, []
    for object_path in objects:
        obj = Object(object_path)
        for index, section_name in enumerate(obj.names):
            if section_name not in addresses:
                continue
            content = bytearray(obj.content(index))
            for at, kind, target in obj.relocations(index):
                value = struct.unpack_from(">I", content, at)[0]
                destination = (
                    target["value"] + addresses.get(obj.names[target["section"]], 0)
                    if target["section"] < len(obj.names)
                    else target["value"]
                )
                if kind == 2:
                    value += destination
                elif kind == 4:
                    value = value & 0xFC000000 | ((value & 0x3FFFFFF) + (destination >> 2)) & 0x3FFFFFF
                elif kind == 5:
                    low_at = next(
                        i for i, k, s in obj.relocations(index) if i > at and k == 6 and s["index"] == target["index"]
                    )
                    low = struct.unpack_from(">I", content, low_at)[0] & 0xFFFF
                    effective = ((value & 0xFFFF) << 16) + (low - 65536 if low & 0x8000 else low) + destination
                    value = value & 0xFFFF0000 | (effective + 0x8000) >> 16 & 0xFFFF
                elif kind == 6:
                    value = value & 0xFFFF0000 | (value + destination) & 0xFFFF
                struct.pack_into(">I", content, at, value & 0xFFFFFFFF)
            contents[section_name] = contents.get(section_name, b"") + content
        for table in obj.symbols.values():
            for symbol in table:
                if 0 < symbol["section"] < len(obj.names) and obj.names[symbol["section"]] in addresses:
                    section_name = obj.names[symbol["section"]]
                    symbols.append(
                        (
                            symbol["name"],
                            section_name,
                            symbol["value"] + addresses[section_name],
                            symbol["size"],
                            symbol["info"],
                        )
                    )
    return write_object(path, contents, symbols, addresses=addresses, linked=True)
