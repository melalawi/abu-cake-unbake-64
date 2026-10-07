"""Explicit, offline SDK signature catalog; masks describe relocation bits only."""

from __future__ import annotations

import json
import os
import zlib
from dataclasses import dataclass
from pathlib import Path

from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named


@dataclass(frozen=True)
class Signature:
    name: str
    source: str
    words: tuple[int, ...]
    masks: tuple[int, ...]


@dataclass(frozen=True)
class CRCSignature(Signature):
    """n64sym fingerprint of the complete relocation-masked function body."""

    size: int
    crc_head: int
    crc_body: int


def masked(data: bytes, masks: tuple[int, ...]) -> bytes:
    if not any(masks):
        return data
    result = bytearray(data)
    for index, mask in enumerate(masks[: (len(data) + 3) // 4]):
        if mask:
            at = index * 4
            word = int.from_bytes(result[at : at + 4], "big") & (~mask & 0xFFFFFFFF)
            result[at : at + 4] = word.to_bytes(4, "big")
    return bytes(result)


def load(path: Path) -> tuple[Signature, ...]:
    """Read JSON {source, signatures: [{name, words, masks}]} from a supplied SDK corpus.

    Words/masks are hexadecimal strings. A set mask bit is a relocation bit to
    ignore, as in n64sym-style matching. No catalog is downloaded or inferred.
    """
    try:
        document = json.loads(path.read_text())
        source = document["source"]
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source: required SDK/object provenance")
        result: list[Signature] = []
        for row in document["signatures"]:
            name = row["name"]
            if "crc_body" in row:
                size = row["size"]
                masks = tuple(int(mask, 16) for mask in row["masks"])
                head, body = row["crc_head"], row["crc_body"]
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(size, int)
                    or size < 8
                    or size % 4
                    or len(masks) != size // 4
                    or any(mask not in (0, 0xFFFF, 0x03FFFFFF) for mask in masks)
                    or not all(isinstance(crc, int) and 0 <= crc <= 0xFFFFFFFF for crc in (head, body))
                    or not any(mask == 0 for mask in masks)
                ):
                    raise ValueError(f"{name}: invalid masked CRC signature")
                result.append(CRCSignature(name, source, (), masks, size, head, body))
                continue
            words = tuple(int(word, 16) for word in row["words"])
            masks = tuple(int(mask, 16) for mask in row["masks"])
            if not isinstance(name, str) or not name or not words or len(words) != len(masks):
                raise ValueError("name/words/masks: required equal nonempty lengths")
            if any(not 0 <= value <= 0xFFFFFFFF for value in (*words, *masks)):
                raise ValueError(f"{name}: expected 32-bit words/masks")
            if not any(mask == 0 for mask in masks):
                raise ValueError(f"{name}: required fixed instruction anchor")
            result.append(Signature(name, source, words, masks))
        if not result:
            raise ValueError("signatures: required nonempty SDK catalog")
        return tuple(result)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "layout.boundary_signatures.load",
                    f"SDK signatures {path}: {error}",
                    owner="layout.boundary_signatures",
                    stage="boundary",
                ),
            )
        ) from error


def configured() -> tuple[Signature, ...]:
    value = os.environ.get("UNBAKE_BOUNDARY_SIGNATURES")
    if not value:
        raise Held(
            cause_named(
                "UNBAKE_BOUNDARY_SIGNATURES",
                "UNBAKE_BOUNDARY_SIGNATURES: required offline SDK signature catalog",
                owner="layout.boundary_signatures",
                stage="boundary",
            )
        )
    return load(Path(value))


MINIMUM_BODY_SIZE = 16


@dataclass(frozen=True)
class Identification:
    matches: dict[int, Signature]
    withheld: dict[int, tuple[str, ...]]


def identify(data: bytes, begin: int, end: int, signatures: tuple[Signature, ...]) -> Identification:
    """Identify bodies of at least 16 bytes occurring once in the scanned range.

    Short bodies cannot distinguish common instruction idioms. Repeated masked
    bodies cannot identify which occurrence owns the catalog function. Neither
    supplies identity evidence, even when the catalog assigns only one name.
    Withheld offsets include the function name and every specificity reason.
    """
    occurrences: dict[Signature, set[int]] = {}
    groups: dict[tuple[int, ...], dict[int, list[CRCSignature]]] = {}
    for signature in signatures:
        if isinstance(signature, CRCSignature):
            groups.setdefault(signature.masks[:2], {}).setdefault(signature.crc_head, []).append(signature)
    for masks, heads in groups.items():
        for start in range(begin + (-begin % 4), end - 7, 4):
            head = zlib.crc32(masked(data[start : start + 8], masks))
            for candidate in heads.get(head, ()):
                stop = start + candidate.size
                if stop <= end and zlib.crc32(masked(data[start:stop], candidate.masks)) == candidate.crc_body:
                    occurrences.setdefault(candidate, set()).add(start)
    for signature in signatures:
        if isinstance(signature, CRCSignature):
            continue
        anchor = next(index for index, mask in enumerate(signature.masks) if mask == 0)
        needle = signature.words[anchor].to_bytes(4, "big")
        cursor = begin
        while (found := data.find(needle, cursor, end)) >= 0:
            cursor = found + 1
            start = found - anchor * 4
            if start < begin or start % 4 or start + len(signature.words) * 4 > end:
                continue
            if all(
                (int.from_bytes(data[start + index * 4 : start + index * 4 + 4], "big") ^ word) & (~mask & 0xFFFFFFFF)
                == 0
                for index, (word, mask) in enumerate(zip(signature.words, signature.masks, strict=True))
            ):
                occurrences.setdefault(signature, set()).add(start)
    candidates: dict[int, list[Signature]] = {}
    withheld: dict[int, set[str]] = {}
    for signature, offsets in occurrences.items():
        size = signature.size if isinstance(signature, CRCSignature) else len(signature.words) * 4
        reasons = []
        if size < MINIMUM_BODY_SIZE:
            reasons.append(f"{signature.name}: body size {size} below minimum {MINIMUM_BODY_SIZE}")
        if len(offsets) > 1:
            reasons.append(f"{signature.name}: masked body occurs at {len(offsets)} candidate offsets")
        for start in offsets:
            if reasons:
                withheld.setdefault(start, set()).update(reasons)
            else:
                candidates.setdefault(start, []).append(signature)
    result: dict[int, Signature] = {}
    for start, entries in candidates.items():
        names = {signature.name for signature in entries}
        if len(names) > 1:
            withheld.setdefault(start, set()).add("ambiguous catalog names: " + ", ".join(sorted(names)))
        else:
            result[start] = entries[0]
            withheld.pop(start, None)
    return Identification(result, {start: tuple(sorted(reasons)) for start, reasons in withheld.items()})


def matches(data: bytes, begin: int, end: int, signatures: tuple[Signature, ...]) -> dict[int, Signature]:
    """Supply only specific identities for boundary evidence."""
    return identify(data, begin, end, signatures).matches
