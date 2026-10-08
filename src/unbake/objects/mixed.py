"""Native relocation preparation and extraction for explicit compiler table subextents.

The generated Make route uses this module from its existing content-pinned tool
bundle. It never reads a ROM or replaces native linker relocation with copied
ROM bytes. Only the derived object receives the selected pointer encoding bias;
its local relocation entries and real .text placement remain with the linker.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from unbake import atomic
from unbake.objects import rodata
from unbake.objects.elf import Object


def table(obj: Object, section: str, offset: int, size: int, function: str) -> bytes:
    text = obj.section(".text")
    if section not in {".rdata", ".rodata"} or offset < 0 or size <= 0:
        raise ValueError("explicit compiler table section and positive bounds required")
    functions = [
        s
        for symbols in obj.symbols.values()
        for s in symbols
        if s["section"] == text and s["info"] & 15 == 2 and s["name"] == function
    ]
    if (
        text is None
        or len(functions) != 1
        or functions[0]["value"] != 0
        or functions[0]["size"] != len(obj.content(text))
    ):
        raise ValueError("table needs the complete native published function")
    if rodata.Pool(section, offset, size) not in rodata.pools(obj, section, True):
        raise ValueError("selected extent must be one complete local-text relocation run")
    index = obj.section(section)
    assert index is not None
    content = obj.content(index)
    for at, _, symbol in obj.relocations(index):
        if offset <= at < offset + size:
            target = int.from_bytes(content[at : at + 4], "big") + symbol["value"]
            if not 0 <= target < len(obj.content(text)) or target % 4:
                raise ValueError("table relocation target lies outside aligned native text")
    return content[offset : offset + size]


def prepare(
    original: Object, placed: Object, output: Path, *, section: str, offset: int, size: int, bias: int, function: str
) -> None:
    if output.resolve() in {original.path.resolve(), placed.path.resolve()}:
        raise ValueError("derived output must preserve the original and placed objects")
    table(original, section, offset, size, function)
    table(placed, section, offset, size, function)
    index, before = placed.section(section), original.section(section)
    assert index is not None and before is not None
    if (
        bias not in {0, 0x80000000}
        or placed.content(index) != original.content(before)
        or placed.relocations(index) != original.relocations(before)
    ):
        raise ValueError("placed table differs from its real compiler object or unsupported pointer bias")
    # Adjust REL addends on a derived object. The actual local .text symbols,
    # relocation entries and code bytes stay intact for the native linker.
    for at, _, _ in placed.relocations(index):
        if offset <= at < offset + size:
            location = placed.sections[index][4] + at
            word = struct.unpack_from(">I", placed.data, location)[0]
            struct.pack_into(">I", placed.data, location, (word - bias) & 0xFFFFFFFF)
    atomic.write(output, bytes(placed.data))


def extract(
    original: Object,
    final: Object,
    code: bytes,
    *,
    section: str,
    offset: int,
    size: int,
    bias: int,
    function: str,
    text: int,
) -> bytes:
    table(original, section, offset, size, function)
    original_text = original.section(".text")
    assert original_text is not None
    data_index, text_index = final.section(".data"), final.section(".text")
    if (
        bias not in {0, 0x80000000}
        or struct.unpack_from(">H", final.data, 16)[0] != 2
        or data_index is None
        or text_index is None
        or final.sections[data_index][1] != 1
        or not final.sections[data_index][2] & 2
        or final.sections[text_index][1] != 1
        or not final.sections[text_index][2] & 4
        or final.sections[text_index][3] != text
        or final.content(text_index) != code
        or len(code) != len(original.content(original_text))
        or final.relocations(data_index)
        or final.relocations(text_index)
    ):
        raise ValueError("complete final relocated table and exact published native text required")
    material = rodata.relocated(original, section, text)[offset : offset + size]
    expected = b"".join(struct.pack(">I", (word[0] - bias) & 0xFFFFFFFF) for word in struct.iter_unpack(">I", material))
    linked = final.content(data_index)[offset : offset + size]
    if len(linked) != size or linked != expected:
        raise ValueError("final table differs from native local-text relocation results")
    return linked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "extract"))
    for name in ("original", "placed", "final", "code", "output"):
        parser.add_argument("--" + name, type=Path, required=name in {"original", "output"})
    parser.add_argument("--section", required=True)
    parser.add_argument("--function", required=True)
    for name in ("offset", "size", "bias", "text"):
        parser.add_argument("--" + name, type=lambda value: int(value, 0), required=True)
    args = parser.parse_args()
    options = dict(section=args.section, offset=args.offset, size=args.size, bias=args.bias, function=args.function)
    try:
        if args.operation == "prepare":
            if args.placed is None:
                parser.error("prepare requires --placed")
            prepare(Object(args.original), Object(args.placed), args.output, **options)
        else:
            if args.final is None or args.code is None:
                parser.error("extract requires --final and --code")
            atomic.write(
                args.output,
                extract(Object(args.original), Object(args.final), args.code.read_bytes(), text=args.text, **options),
            )
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
