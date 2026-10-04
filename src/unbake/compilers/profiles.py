"""Pinned, calibrated instruction features; no release inference from strings."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from unbake.compilers import registry as toolchain
from unbake.config import Held


@dataclass(frozen=True)
class Exemplar:
    name: str
    words: tuple[int, ...]
    masks: tuple[int, ...]

    def matches(self, words: tuple[int, ...]) -> bool:
        # Splat may retain alignment padding following the last delay slot.
        if len(words) < len(self.words) or any(words[len(self.words) :]):
            return False
        return all(
            not ((expected ^ actual) & ~mask)
            for expected, actual, mask in zip(self.words, words, self.masks, strict=False)
        )


@dataclass(frozen=True)
class Profile:
    id: str
    abi: str
    features: tuple[str, ...]
    exemplars: tuple[Exemplar, ...]


FEATURES = {
    "addu_moves",
    "or_moves",
    "signed_char_load",
    "unsigned_char_load",
    "frame_restore_before_return",
    "frame_restore_delay",
}
OPPOSITE = {
    "addu_moves": "or_moves",
    "or_moves": "addu_moves",
    "signed_char_load": "unsigned_char_load",
    "unsigned_char_load": "signed_char_load",
    "frame_restore_before_return": "frame_restore_delay",
    "frame_restore_delay": "frame_restore_before_return",
}


def read() -> tuple[dict[str, Profile], dict[str, Any]]:
    data = toolchain._read(toolchain.REGISTRY_PATH)
    rules = data.get("fingerprint_policy")
    if not isinstance(rules, dict) or rules.get("schema") != 1:
        raise Held("setup", "setup.compiler_profile: fingerprint_policy.schema: expected 1")
    for key in ("minimum_moves", "agreement_numerator", "agreement_denominator"):
        if type(rules.get(key)) is not int or rules[key] <= 0:
            raise Held("setup", f"setup.compiler_profile: fingerprint_policy.{key}: expected positive integer")
    if rules["agreement_numerator"] > rules["agreement_denominator"]:
        raise Held("setup", "setup.compiler_profile: fingerprint_policy.agreement_numerator: exceeds denominator")
    if rules.get("rank") != [
        "exclusive_exemplars",
        "matching_exemplars",
        "feature_agreement",
        "negative_contradictions",
    ]:
        raise Held("setup", "setup.compiler_profile: fingerprint_policy.rank: unsupported ordering")
    specs = toolchain.registry()
    output = {}
    tables = data.get("fingerprints", {})
    if not isinstance(tables, dict):
        raise Held("setup", "setup.compiler_profile: fingerprints: expected table")
    for ident, table in tables.items():
        label = f"setup.compiler_profile: {ident}"
        if ident not in specs or not isinstance(table, dict):
            raise Held("setup", f"{label}: unknown registry ID or invalid profile")
        if table.get("abi") != "gp32":
            raise Held("setup", f"{label}.abi: supported ABI must be gp32")
        spec = specs[ident]
        if table.get("compiler_sha256") != spec.pins[spec.cc] or table.get("calibration_cflags") != list(spec.cflags):
            raise Held("setup", f"{label}.compiler_sha256: calibration pins/flags differ from registry")
        source = table.get("calibration_source")
        if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != table.get(
            "calibration_source_sha256"
        ):
            raise Held("setup", f"{label}.calibration_source_sha256: source digest differs")
        features = table.get("features")
        if not isinstance(features, list) or not features or any(feature not in FEATURES for feature in features):
            raise Held("setup", f"{label}.features: missing or unsupported features")
        exemplars = []
        entries = table.get("exemplars")
        if not isinstance(entries, list) or not entries:
            raise Held("setup", f"{label}.exemplars: expected nonempty array")
        for entry in entries:
            try:
                name = entry["name"]
                words = tuple(int(word, 16) for word in entry["words"])
                masks = tuple(int(mask, 16) for mask in entry["relocation_masks"])
                if (
                    not isinstance(name, str)
                    or not name
                    or not words
                    or len(words) != len(masks)
                    or any(not 0 <= word <= 0xFFFFFFFF for word in (*words, *masks))
                    or any(mask not in (0, 0xFFFF, 0x03FFFFFF, 0xFFFFFFFF) for mask in masks)
                ):
                    raise ValueError("invalid instruction or relocation mask")
            except (KeyError, TypeError, ValueError) as error:
                raise Held("setup", f"{label}.exemplars: {error}") from error
            exemplars.append(Exemplar(name, words, masks))
        output[ident] = Profile(ident, table["abi"], tuple(features), tuple(exemplars))
    return output, rules


def measure(data: bytes) -> dict[str, int]:
    if len(data) % 4:
        raise Held("setup", "setup.compiler_proposal: partial instruction word")
    words = tuple(word for (word,) in struct.iter_unpack(">I", data))
    measured = dict.fromkeys(FEATURES, 0)
    for index, word in enumerate(words):
        op, rs, rt, rd = word >> 26, word >> 21 & 31, word >> 16 & 31, word >> 11 & 31
        if op == 0 and not (word >> 6 & 31) and rd and bool(rs) != bool(rt):
            if word & 63 == 0x21:
                measured["addu_moves"] += 1
            elif word & 63 == 0x25:
                measured["or_moves"] += 1
        if op == 0x20:
            measured["signed_char_load"] += 1
        elif op == 0x24:
            measured["unsigned_char_load"] += 1
        # Positive addiu sp,sp in a return delay slot versus before jr ra/nop.
        if word == 0x03E00008 and index + 1 < len(words):
            following = words[index + 1]
            if following & 0xFFFF0000 == 0x27BD0000 and 0 < following & 0xFFFF < 0x8000:
                measured["frame_restore_delay"] += 1
            if (
                index
                and following == 0
                and words[index - 1] & 0xFFFF0000 == 0x27BD0000
                and 0 < words[index - 1] & 0xFFFF < 0x8000
            ):
                measured["frame_restore_before_return"] += 1
    return measured


def rank(measured: dict[str, int], matches: dict[str, list[str]], profiles: dict[str, Profile]) -> dict[str, list[int]]:
    result = {}
    for ident, profile in profiles.items():
        exclusive = sum(
            not any(name in other for other_id, other in matches.items() if other_id != ident)
            for name in matches[ident]
        )
        agreement = sum(measured[feature] for feature in profile.features)
        contradictions = sum(measured[OPPOSITE[feature]] for feature in profile.features)
        result[ident] = [exclusive, len(matches[ident]), agreement, -contradictions]
    return result
