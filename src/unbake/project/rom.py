"""Normalised cartridge bytes, header facts and opcode similarity."""

import hashlib
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.project import header

if TYPE_CHECKING:
    from unbake.layout.split import Function


@dataclass(frozen=True)
class Rom:
    path: Path
    data: bytes | None
    header: header.Header
    sha1: str

    def image(self) -> bytes:
        """Read a normalized image only while its version is being consumed."""
        if self.data is not None:
            return self.data
        try:
            data = normalise(self.path.read_bytes())
        except OSError as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(f"{self.path}", f"{self.path}: {error}", owner="project.rom", stage="setup"),
                )
            ) from error
        if hashlib.sha1(data).hexdigest() != self.sha1:
            raise Held(
                cause_named(
                    "setup.rom_changed",
                    f"setup.rom_changed: {self.path}: sha1 changed after census",
                    owner="project.rom",
                    stage="setup",
                )
            )
        return data


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


def load(path: Path, *, retain_data: bool = True) -> Rom:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise Held(
            capture(error, cause=cause_named(f"{path}", f"{path}: {error}", owner="project.rom", stage="setup"))
        ) from error
    try:
        data = normalise(data)
    except Held as error:
        field, reason = error.reason.split(":", 1)
        raise Held(
            capture(
                error, cause=cause_named(f"{field}", f"{field}: {path}:{reason}", owner="project.rom", stage="setup")
            )
        ) from error
    try:
        facts = header.parse(data, header.RETAIL)
    except Held as error:
        field, reason = error.reason.split(":", 1)
        raise Held(
            capture(
                error, cause=cause_named(f"{field}", f"{field}: {path}:{reason}", owner="project.rom", stage="setup")
            )
        ) from error
    return Rom(path, data if retain_data else None, facts, hashlib.sha1(data).hexdigest())


def normalise(data: bytes) -> bytes:
    """Return big-endian bytes from a z64, v64 or n64 image."""
    magic = data[:4]
    widths = {bytes.fromhex("80371240"): 1, bytes.fromhex("37804012"): 2, bytes.fromhex("40123780"): 4}
    if magic not in widths:
        raise Held(
            cause_named(
                "rom.magic", f"rom.magic: not an N64 ROM (magic {magic.hex()})", owner="project.rom", stage="setup"
            )
        )
    width = widths[magic]
    if len(data) < 0x40 or len(data) % 4:
        raise Held(
            cause_named(
                "rom.size", f"rom.size: truncated N64 ROM (size 0x{len(data):X})", owner="project.rom", stage="setup"
            )
        )
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
        raise Held(
            cause_named(
                "inventories",
                "inventories: required mapping of ROM paths to detected code ranges",
                owner="project.rom",
                stage="setup",
            )
        )
    signatures: dict[Rom, frozenset[bytes]] = {}
    for cartridge in cartridges:
        image = cartridge.image()
        functions = inventories.get(cartridge.path)
        if not functions:
            raise Held(
                cause_named(
                    "setup.same_game.code_ranges",
                    f"setup.same_game.code_ranges: {cartridge.path}: detected code ranges missing",
                    owner="project.rom",
                    stage="setup",
                )
            )
        signature: set[bytes] = set()
        for function in functions:
            start, end = function.start, function.end
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(image)
                or start % 4
                or end % 4
            ):
                raise Held(
                    cause_named(
                        f"{cartridge.path}",
                        f"{cartridge.path}: detected code range {start!r}-{end!r}: invalid word range",
                        owner="project.rom",
                        stage="setup",
                    )
                )
            signature.update(shingles(image[start:end]))
        if not signature:
            raise Held(
                cause_named(
                    "setup.same_game.code_ranges",
                    f"setup.same_game.code_ranges: {cartridge.path}: detected code shingles missing",
                    owner="project.rom",
                    stage="setup",
                )
            )
        signatures[cartridge] = frozenset(signature)
        del image
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
        raise Held(cause_named("setup.roms", "setup.roms: no ROM files", owner="project.rom", stage="setup"))
    if type(threshold) not in (int, float) or not 0 < threshold <= 1:
        raise Held(
            cause_named(
                "policy.same_game_similarity",
                "policy.same_game_similarity: required fraction in (0, 1]",
                owner="project.rom",
                stage="setup",
            )
        )
    if reference not in cartridges:
        raise Held(
            cause_named(
                "project.names_from", "project.names_from: reference ROM is absent", owner="project.rom", stage="setup"
            )
        )
    seen: dict[str, Path] = {}
    for cartridge in cartridges:
        if cartridge.sha1 in seen:
            raise Held(
                cause_named(
                    "setup.roms.duplicate_sha1",
                    (
                        f"setup.roms.duplicate_sha1: {cartridge.path}: duplicate sha1 "
                        f"{cartridge.sha1} ({seen[cartridge.sha1]})"
                    ),
                    owner="project.rom",
                    stage="setup",
                )
            )
        seen[cartridge.sha1] = cartridge.path
        if (cartridge.header.category, cartridge.header.game_code) != (
            reference.header.category,
            reference.header.game_code,
        ):
            raise Held(
                cause_named(
                    "setup.same_game.game_code",
                    (
                        f"setup.same_game.game_code: {cartridge.path}: game code "
                        f"{cartridge.header.category + cartridge.header.game_code} differs from "
                        f"{reference.header.category + reference.header.game_code} ("
                        f"{reference.path})"
                    ),
                    owner="project.rom",
                    stage="setup",
                )
            )
    matrix = similarity_matrix(cartridges, inventories) if matrix is None else matrix
    for cartridge in cartridges:
        for other in cartridges:
            if matrix[cartridge, other] < threshold:
                raise Held(
                    cause_named(
                        "setup.same_game.similarity",
                        (
                            f"setup.same_game.similarity: {cartridge.path} vs {other.path}: code "
                            f"similarity {matrix[cartridge, other]:.6f} below {threshold:.6f}; "
                            f"reference {reference.path}"
                        ),
                        owner="project.rom",
                        stage="setup",
                    )
                )
    return matrix


def stem(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise Held(cause_named(f"{label}", f"{label}: expected a valid file stem", owner="project.rom", stage="setup"))
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
                cause_named(
                    "setup.roms.duplicate_version",
                    (
                        f"setup.roms.duplicate_version: {cartridge.path}: VERSION {name} also "
                        f"names {seen[name]}; supply ROMs with distinct header labels"
                    ),
                    owner="project.rom",
                    stage="setup",
                )
            )
        names[cartridge.path] = name
        seen[name] = cartridge.path
    for unused in renames.keys() - used:
        raise Held(
            cause_named(
                "project.rom.version_names",
                f"--version-name {unused}: unknown derived VERSION",
                owner="project.rom",
                stage="setup",
            )
        )
    return names
