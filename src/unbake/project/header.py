"""N64 cartridge header fields, IPL3 identification and integrity checks."""

import re
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named

DESTINATIONS = dict(
    zip(
        "7ABCDEFGHIJKLNPSUWXYZ",
        (
            "beta",
            "all",
            "br",
            "cn",
            "de",
            "us",
            "fr",
            "gw-ntsc",
            "nl",
            "it",
            "jp",
            "kr",
            "gw-pal",
            "ca",
            "eu",
            "es",
            "au",
            "nordic",
            "eu-x",
            "eu-y",
            "eu-z",
        ),
        strict=False,
    )
)
CATEGORIES = {"N": "cartridge", "D": "64dd-disk", "C": "expandable", "E": "64dd-expansion", "Z": "aleck64"}
RETAIL = {
    0x6170A4A1: "6101",
    0x009E9EA3: "7102",
    0x90BB6CB5: "6102/7101",
    0x0B050EE0: "6103/7103",
    0x98BC2C86: "6105/7105",
    0xACC8580A: "6106/7106",
}
SEEDS = {
    "6101": 0xF8CA4DDC,
    "7102": 0xF8CA4DDC,
    "6102/7101": 0xF8CA4DDC,
    "6103/7103": 0xA3886759,
    "6105/7105": 0xDF26F436,
    "6106/7106": 0x1FEA617A,
}
MASK = 0xFFFFFFFF


@dataclass(frozen=True)
class Fields:
    pi: int
    clock_rate: int
    entry: int
    libultra: int
    release_revision: int
    release_letter: str
    crc1: int
    crc2: int
    title: str
    category: str
    game_code: str
    region: str
    revision: int


@dataclass(frozen=True)
class Header(Fields):
    cic: str


def decode(data: bytes) -> Fields:
    """Decode the 64-byte big-endian header without claiming integrity."""
    if len(data) < 0x40:
        raise Held(
            cause_named("header.size", "header.size: requires 0x40 bytes", owner="project.header", stage="header")
        )
    pi, clock, entry, release, crc1, crc2 = struct.unpack_from(">6I", data)
    if pi != 0x80371240:
        raise Held(
            cause_named(
                "header.pi",
                f"header.pi: expected big-endian N64 magic, got 0x{pi:08X}",
                owner="project.header",
                stage="header",
            )
        )
    category, region = chr(data[0x3B]), chr(data[0x3E])
    if category not in CATEGORIES:
        raise Held(
            cause_named(
                "header.category",
                f"header.category: unknown code 0x{data[59]:02X}",
                owner="project.header",
                stage="header",
            )
        )
    if region not in DESTINATIONS:
        raise Held(
            cause_named(
                "header.destination",
                f"header.destination: unknown code 0x{data[62]:02X}",
                owner="project.header",
                stage="header",
            )
        )
    try:
        title = data[0x20:0x34].decode("ascii").rstrip(" \0")
        code = data[0x3C:0x3E].decode("ascii")
    except UnicodeDecodeError as error:
        field = "title" if any(b > 127 for b in data[0x20:0x34]) else "game_code"
        raise Held(
            capture(
                error,
                cause=cause_named(
                    f"header.{field}", f"header.{field}: expected ASCII", owner="project.header", stage="header"
                ),
            )
        ) from error
    if not title or any(ord(character) < 32 for character in title):
        raise Held(
            cause_named(
                "header.title",
                "header.title: expected nonempty printable ASCII",
                owner="project.header",
                stage="header",
            )
        )
    if not re.fullmatch(r"[A-Z0-9]{2}", code):
        raise Held(
            cause_named(
                "header.game_code",
                "header.game_code: expected two uppercase ASCII letters or digits",
                owner="project.header",
                stage="header",
            )
        )
    return Fields(
        pi,
        clock,
        entry,
        release,
        release >> 8,
        chr(release & 255),
        crc1,
        crc2,
        title,
        category,
        code,
        region,
        data[0x3F],
    )


def checksum(data: bytes, cic: str) -> tuple[int, int]:
    """Recompute the IPL3 checksum over the first MiB of program bytes."""
    if cic not in SEEDS:
        raise Held(
            cause_named("header.cic", f"header.cic: unsupported variant {cic}", owner="project.header", stage="header")
        )
    if len(data) < 0x101000:
        raise Held(
            cause_named(
                "header.crc_data",
                "header.crc_data: requires bytes through 0x101000",
                owner="project.header",
                stage="header",
            )
        )
    carry = total = rotated = mixed = folded = boot = SEEDS[cic]
    for offset in range(0x1000, 0x101000, 4):
        word = struct.unpack_from(">I", data, offset)[0]
        new_total = (total + word) & MASK
        carry = (carry + (new_total < total)) & MASK
        total = new_total
        mixed ^= word
        shift = word & 31
        rotation = ((word << shift) | (word >> ((32 - shift) & 31))) & MASK
        rotated = (rotated + rotation) & MASK
        folded ^= rotation if folded > word else total ^ word
        boot_word = struct.unpack_from(">I", data, 0x750 + (offset & 255))[0] if cic == "6105/7105" else rotated
        boot = (boot + (boot_word ^ word)) & MASK
    if cic == "6103/7103":
        return ((total ^ carry) + mixed) & MASK, ((rotated ^ folded) + boot) & MASK
    if cic == "6106/7106":
        return (total * carry + mixed) & MASK, (rotated * folded + boot) & MASK
    return total ^ carry ^ mixed, rotated ^ folded ^ boot


def parse(data: bytes, bootcodes: Mapping[int, str]) -> Header:
    """Decode a normalised ROM and refuse missing IPL3 or mismatching CRCs."""
    if not isinstance(bootcodes, Mapping):
        raise Held(
            cause_named(
                "header.bootcodes",
                "header.bootcodes: required mapping of IPL3 CRC32 to CIC",
                owner="project.header",
                stage="header",
            )
        )
    fields = decode(data)
    if len(data) < 0x1000:
        raise Held(
            cause_named(
                "header.ipl3", "header.ipl3: requires bytes through 0x1000", owner="project.header", stage="header"
            )
        )
    boot_crc = zlib.crc32(data[0x40:0x1000])
    cic = bootcodes.get(boot_crc)
    if cic is None:
        raise Held(
            cause_named(
                "header.cic",
                f"header.cic: unknown IPL3 CRC32 0x{boot_crc:08X}",
                owner="project.header",
                stage="header",
            )
        )
    actual = checksum(data, cic)
    for name, expected, computed in zip(("crc1", "crc2"), (fields.crc1, fields.crc2), actual, strict=False):
        if expected != computed:
            raise Held(
                cause_named(
                    f"header.{name}",
                    f"header.{name}: stored 0x{expected:08X}, computed 0x{computed:08X} ({cic})",
                    owner="project.header",
                    stage="header",
                )
            )
    return Header(**vars(fields), cic=cic)


def label(header: Fields) -> str:
    """Return the destination label with a single nonzero revision suffix."""
    if header.region not in DESTINATIONS:
        raise Held(
            cause_named(
                "header.destination",
                f"header.destination: unknown code {header.region!r}",
                owner="project.header",
                stage="header",
            )
        )
    return DESTINATIONS[header.region] + (f"-rev{header.revision}" if header.revision else "")
