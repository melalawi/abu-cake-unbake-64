"""Generate two synthetic cartridges with three short MIPS functions each."""

import argparse
import struct
from pathlib import Path

VERSIONS = ("us", "us-rev1")


def rom_bytes(version: str) -> bytes:
    if version not in VERSIONS:
        raise ValueError(f"unknown VERSION {version}")
    header = bytearray(0x40)
    struct.pack_into(">4I", header, 0, 0x80371240, 0x0000000F, 0x80001000, 0)
    header[0x20:0x34] = b"UNBAKE FIXTURE      "
    header[0x3B:0x40] = b"NUB\x45\x00"
    constants = (1, 2 if version == "us" else 4, 3)
    functions = b"".join(struct.pack(">3I", 0x24020000 | value, 0x03E00008, 0) for value in constants)
    return bytes(header) + functions


def write_roms(output: Path) -> list[Path]:
    output = output / "roms"
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for version in VERSIONS:
        path = output / f"baserom.{version}.z64"
        path.write_bytes(rom_bytes(version))
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in write_roms(args.output):
        print(path)


if __name__ == "__main__":
    main()
