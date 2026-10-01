"""Normalised cartridge bytes, header facts and opcode similarity."""

import hashlib
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.project import header
from unbake.project.config import Held

if TYPE_CHECKING:
    from unbake.layout.split import Function


@dataclass(frozen=True)
class Rom:
    path: Path
    data: bytes
    header: header.Header
    sha1: str


def shingles(data: bytes) -> frozenset[bytes]:
    """Eight instructions, discarding registers, immediates and jump targets."""
    end = len(data) - len(data) % 4
    tokens = bytearray()
    for (word,) in struct.iter_unpack(">I", data[:end]):
        opcode = word >> 26
        has_function = opcode == 0 or (opcode in (16, 17, 18) and (word >> 21) & 31 >= 16)
        token = opcode << 6 | (word & 63 if has_function else 0)
        tokens.extend(token.to_bytes(2, "big"))
    return frozenset(bytes(tokens[index : index + 16]) for index in range(0, len(tokens) - 15, 2))


def load(path: Path) -> Rom:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise Held("setup", f"{path}: {error}") from error
    try:
        data = normalise(data)
    except Held as error:
        field, reason = error.reason.split(":", 1)
        raise Held("setup", f"{field}: {path}:{reason}") from error
    try:
        facts = header.parse(data, header.RETAIL)
    except Held as error:
        field, reason = error.reason.split(":", 1)
        raise Held("setup", f"{field}: {path}:{reason}") from error
    return Rom(path, data, facts, hashlib.sha1(data).hexdigest())


def normalise(data: bytes) -> bytes:
    """Return big-endian bytes from a z64, v64 or n64 image."""
    magic = data[:4]
    widths = {bytes.fromhex("80371240"): 1, bytes.fromhex("37804012"): 2, bytes.fromhex("40123780"): 4}
    if magic not in widths:
        raise Held("setup", f"rom.magic: not an N64 ROM (magic {magic.hex()})")
    width = widths[magic]
    if len(data) < 0x40 or len(data) % 4:
        raise Held("setup", f"rom.size: truncated N64 ROM (size 0x{len(data):X})")
    if width != 1:
        normalised = bytearray(len(data))
        for index in range(width):
            normalised[index::width] = data[width - index - 1 :: width]
        data = bytes(normalised)
    return data


def similarity_matrix(
    cartridges: list[Rom], inventories: Mapping[Path, Sequence["Function"]]
) -> dict[tuple[Rom, Rom], float]:
    """Compute every symmetric pair using measured code, excluding assets/IPL3."""
    if not isinstance(inventories, Mapping):
        raise Held("setup", "inventories: required mapping of ROM paths to detected code ranges")
    signatures: dict[Rom, frozenset[bytes]] = {}
    for cartridge in cartridges:
        functions = inventories.get(cartridge.path)
        if not functions:
            raise Held("setup", f"setup.same_game.code_ranges: {cartridge.path}: detected code ranges missing")
        signature: set[bytes] = set()
        for function in functions:
            start, end = function.start, function.end
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(cartridge.data)
                or start % 4
                or end % 4
            ):
                raise Held("setup", f"{cartridge.path}: detected code range {start!r}-{end!r}: invalid word range")
            signature.update(shingles(cartridge.data[start:end]))
        if not signature:
            raise Held("setup", f"setup.same_game.code_ranges: {cartridge.path}: detected code shingles missing")
        signatures[cartridge] = frozenset(signature)
    matrix = {(cartridge, cartridge): 1.0 for cartridge in cartridges}
    for index, cartridge in enumerate(cartridges):
        for other in cartridges[index + 1 :]:
            left, right = signatures[cartridge], signatures[other]
            matrix[cartridge, other] = matrix[other, cartridge] = len(left & right) / len(left | right)
    return matrix


def same_game(
    cartridges: list[Rom],
    inventories: Mapping[Path, Sequence["Function"]],
    threshold: float,
    *,
    reference: Rom,
    matrix: Mapping[tuple[Rom, Rom], float] | None = None,
) -> Mapping[tuple[Rom, Rom], float]:
    """Require header identity and similarity to the reference and every peer."""
    if not cartridges:
        raise Held("setup", "setup.roms: no ROM files")
    if type(threshold) not in (int, float) or not 0 < threshold <= 1:
        raise Held("setup", "policy.same_game_similarity: required fraction in (0, 1]")
    if reference not in cartridges:
        raise Held("setup", "project.names_from: reference ROM is absent")
    seen: dict[str, Path] = {}
    for cartridge in cartridges:
        if cartridge.sha1 in seen:
            raise Held(
                "setup",
                f"setup.roms.duplicate_sha1: {cartridge.path}: duplicate sha1 "
                f"{cartridge.sha1} ({seen[cartridge.sha1]})",
            )
        seen[cartridge.sha1] = cartridge.path
        if (cartridge.header.category, cartridge.header.game_code) != (
            reference.header.category,
            reference.header.game_code,
        ):
            raise Held(
                "setup",
                f"setup.same_game.game_code: {cartridge.path}: game code "
                f"{cartridge.header.category + cartridge.header.game_code} differs from "
                f"{reference.header.category + reference.header.game_code} ({reference.path})",
            )
    matrix = similarity_matrix(cartridges, inventories) if matrix is None else matrix
    for cartridge in cartridges:
        for other in cartridges:
            if matrix[cartridge, other] < threshold:
                raise Held(
                    "setup",
                    f"setup.same_game.similarity: {cartridge.path} vs {other.path}: "
                    f"code similarity {matrix[cartridge, other]:.6f} below {threshold:.6f}; "
                    f"reference {reference.path}",
                )
    return matrix


def stem(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise Held("setup", f"{label}: expected a valid file stem")
    return value


def version_names(roms: list[Rom], renames: dict[str, str]) -> dict[Path, str]:
    names: dict[Path, str] = {}
    seen: dict[str, Path] = {}
    used: set[str] = set()
    for cartridge in roms:
        old = header.label(cartridge.header)
        name = stem(renames.get(old, old), f"--version-name {old}")
        if old in renames:
            used.add(old)
        if name in seen:
            raise Held(
                "setup",
                f"setup.roms.duplicate_version: {cartridge.path}: VERSION {name} also names {seen[name]}; "
                "supply ROMs with distinct header labels",
            )
        names[cartridge.path] = name
        seen[name] = cartridge.path
    for unused in renames.keys() - used:
        raise Held("setup", f"--version-name {unused}: unknown derived VERSION")
    return names
