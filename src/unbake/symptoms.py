"""Pure byte comparisons and the closed vocabulary of measured symptoms."""
import re
from collections import Counter
from collections.abc import Sequence
from difflib import SequenceMatcher, unified_diff

import rabbitizer

from unbake.contracts import Json

_BOOLS = frozenset(("bytes_differ", "unproved", "no_measurement", "compile_failed",
                   "unresolved", "register_dominant", "sp_offset_only", "missing_struct_member"))
_COUNTS = frozenset(("repeated_deleted_runs", "float_register_differences",
                    "literal_immediates_off_four", "plateau_probes"))
_DELTAS = frozenset(("size_delta_bytes", "frame_delta_bytes"))
_INTEGER = r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)"


def _lines(data: bytes) -> list[tuple[str, list[str]]]:
    if len(data) % 4:
        return []
    result = []
    for i in range(len(data) // 4):
        text = rabbitizer.Instruction(int.from_bytes(data[4*i:4*i+4], "big"), vram=4*i).disassemble()
        parts = text.split(None, 1)
        result.append((parts[0], [s.strip() for s in parts[1].split(",")] if len(parts) > 1 else []))
    return result


def _integer(token: str) -> int:
    return int(token, 16 if "x" in token.lower() else 10)


def _kind(b: tuple[str, list[str]], t: tuple[str, list[str]]) -> str:
    if b[0] != t[0] or len(b[1]) != len(t[1]):
        return ""
    pairs = [(x, y) for x, y in zip(b[1], t[1], strict=True) if x != y]
    tokens = [token for pair in pairs for token in pair]
    if tokens and all(re.fullmatch(r"\$[\w]+", s) and s != "$sp"
                      and not re.fullmatch(r"\$f\d+", s) for s in tokens):
        return "register"
    if tokens and all(re.fullmatch(r"\$f\d+", s) for s in tokens):
        return "float"
    if tokens and all(re.fullmatch(_INTEGER + r"\(\$sp\)", s) for s in tokens):
        return "sp"
    if len(pairs) == 1 and all(re.fullmatch(_INTEGER, s) for s in tokens):
        delta = abs(_integer(tokens[0]) - _integer(tokens[1]))
        if 0 < delta <= 16 and delta % 4 == 0:
            return "off-four"
    return ""


def _frame(lines: list[tuple[str, list[str]]]) -> int | None:
    for mnemonic, operands in lines:
        if (mnemonic == "addiu" and len(operands) == 3 and operands[:2] == ["$sp", "$sp"]
                and re.fullmatch(_INTEGER, operands[2]) and _integer(operands[2]) < 0):
            return -_integer(operands[2])
    return None


def measure(built: bytes, target: bytes, section: str) -> Json:
    facts = {"bytes_differ": built != target}
    if len(built) != len(target):
        facts["size_delta_bytes"] = len(built) - len(target)
    if section != ".text" or len(built) % 4 or len(target) % 4:
        return facts
    b, t = _lines(built), _lines(target)
    ops = SequenceMatcher(None, [m for m, _ in b], [m for m, _ in t], autojunk=False).get_opcodes()
    lengths = Counter(i2-i1 for op, i1, i2, _, _ in ops if op == "delete")
    repeated = sum(count for count in lengths.values() if count >= 2)
    if repeated >= 2:
        facts["repeated_deleted_runs"] = repeated
    pairs = [(x, y) for op, i1, i2, j1, j2 in ops
             if op == "equal" or (op == "replace" and i2-i1 == j2-j1)
             for x, y in zip(b[i1:i2], t[j1:j2], strict=True) if x != y]
    kinds = Counter(_kind(x, y) for x, y in pairs)
    if pairs:
        facts["register_dominant"] = kinds["register"] / len(pairs) >= 0.8
        facts["sp_offset_only"] = kinds["sp"] == len(pairs)
    for kind, key in (("float", "float_register_differences"), ("off-four", "literal_immediates_off_four")):
        if kinds[kind]:
            facts[key] = kinds[kind]
    bf, tf = _frame(b), _frame(t)
    if bf is not None and tf is not None and bf != tf:
        facts["frame_delta_bytes"] = tf - bf
    return facts


def score(built: bytes, target: bytes) -> float:
    if built == target:
        return 1.0
    if not target:
        return 0.0
    step = 4 if len(built) % 4 == len(target) % 4 == 0 else 1
    b, t = ([data[i:i+step] for i in range(0, len(data), step)] for data in (built, target))
    matched = sum(block.size for block in SequenceMatcher(None, b, t, autojunk=False).get_matching_blocks())
    return max(0.0, min(matched / len(t), 1.0 - 2**-53))


def from_missing(missing: Sequence[str]) -> Json:
    facts = {}
    for gap in missing:
        match = re.search(r"size (\d+) != (\d+)", gap)
        if match:
            facts["size_delta_bytes"] = int(match[1]) - int(match[2])
        for key, token in (("bytes_differ", "bytes differ"), ("unproved", "unproved"),
                           ("no_measurement", "no measurement"), ("compile_failed", "compile.error"),
                           ("unresolved", "unresolved")):
            if token in gap:
                facts[key] = True
        if re.search(r"has no member|incomplete type", gap):
            facts["missing_struct_member"] = True
    return facts


def merge(facts: Sequence[Json]) -> Json:
    result = {}
    for fact in facts:
        for key, value in fact.items():
            if key in _BOOLS:
                if value:
                    result[key] = True
            elif key in _COUNTS:
                result[key] = result.get(key, 0) + value
            elif key in _DELTAS:
                result.setdefault(key, value)
            elif key == "score":
                result[key] = min(result.get(key, value), value)
    return {key: value for key, value in result.items() if key not in _COUNTS or value != 0}


def diff(built: bytes, target: bytes, vram: int) -> str:
    b, t = ([f"{vram + 4*i:08X}: {m} {', '.join(o)}" for i, (m, o) in enumerate(_lines(data))]
            for data in (built, target))
    return "\n".join(unified_diff(t, b, "target", "built", lineterm="", n=3))
