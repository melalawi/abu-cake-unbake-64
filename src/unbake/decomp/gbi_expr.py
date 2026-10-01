"""Conservative inversion of C bit packing; preserve operand spellings."""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field


class Ambiguous(ValueError):
    """A word does not establish a unique, lossless command encoding."""


def split(text: str, operator: str) -> list[str]:
    depth = 0
    start = 0
    result = []
    for i, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and text.startswith(operator, i):
            if operator in ("|", "&") and (text[i : i + 2] == operator * 2 or text[i - 1 : i] == operator):
                continue
            result.append(text[start:i].strip())
            start = i + len(operator)
    return [*result, text[start:].strip()]


def unwrap(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for i, char in enumerate(text):
            depth += (char == "(") - (char == ")")
            if depth == 0 and i != len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def scalar(text: str) -> str:
    text = unwrap(text)
    while match := re.match(r"^\(\s*(?:u32|s32|unsigned int|int)\s*\)\s*(.+)$", text, re.S):
        text = unwrap(match[1])
    return text


def integer(text: str) -> int | None:
    text = scalar(text)
    if re.fullmatch(r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*", text):
        value = re.sub(r"[uUlL]+$", "", text)
        try:
            digits = value.lstrip("+-")
            base = 16 if "x" in digits.lower() else 8 if len(digits) > 1 and digits.startswith("0") else 10
            return int(value, base)
        except ValueError:
            return None
    for op in ("|", "^", "&", "<<", ">>", "+", "-", "*"):
        parts = split(text, op)
        if len(parts) != 2:
            continue
        a, b = (integer(part) for part in parts)
        if a is None or b is None or (op in ("<<", ">>") and not 0 <= b < 32):
            return None
        operation = {
            "|": operator.or_,
            "&": operator.and_,
            "<<": operator.lshift,
            ">>": operator.rshift,
            "+": operator.add,
            "-": operator.sub,
            "^": operator.xor,
            "*": operator.mul,
        }[op]
        return int(operation(a, b))
    if text.startswith("~"):
        inverted = integer(text[1:])
        return None if inverted is None else ~inverted
    return None


def pure(text: str) -> bool:
    # Reordering bit fields must not reorder evaluations with effects.
    return not re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", text)


@dataclass
class Part:
    shift: int
    width: int
    value: str

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.shift


@dataclass
class Word:
    constant: int = 0
    parts: list[Part] = field(default_factory=list)
    used: int = 0

    @classmethod
    def parse(cls, text: str) -> Word:
        result = cls()
        pending = [text]
        while pending:
            term = scalar(pending.pop(0))
            terms = split(term, "|")
            if len(terms) > 1:
                pending[0:0] = terms
                continue
            value = integer(term)
            if value is not None:
                result.constant |= value & 0xFFFFFFFF
                continue
            call = re.fullmatch(r"_SHIFTL\s*\((.*)\)", term, re.S)
            if call:
                args = split(call[1], ",")
                if len(args) != 3:
                    raise Ambiguous("malformed _SHIFTL")
                shift, width = integer(args[1]), integer(args[2])
                if shift is None or width is None or not 0 < width <= 32 or not 0 <= shift <= 32 - width:
                    raise Ambiguous("computed _SHIFTL layout")
                if not pure(args[0]):
                    raise Ambiguous("operand has evaluation effects")
                fixed = integer(args[0])
                if fixed is not None:
                    result.constant |= (fixed & ((1 << width) - 1)) << shift
                else:
                    result.parts.append(Part(shift, width, args[0]))
                continue
            shifted = split(term, "<<")
            shift = 0
            if len(shifted) == 2:
                maybe_shift = integer(shifted[1])
                if maybe_shift is None or not 0 <= maybe_shift < 32:
                    raise Ambiguous("computed shift")
                shift, term = maybe_shift, unwrap(shifted[0])
            elif len(shifted) != 1:
                raise Ambiguous("multiple shifts")
            masked = split(term, "&")
            width = 32 - shift
            operand = term
            if len(masked) == 2:
                mask = integer(masked[1])
                if mask is None or mask <= 0 or mask & (mask + 1):
                    raise Ambiguous("noncontiguous or computed mask")
                width = min(mask.bit_length(), width)
                operand = masked[0]
            if not pure(operand):
                raise Ambiguous("operand has evaluation effects")
            result.parts.append(Part(shift, width, unwrap(operand)))
        masks = 0
        for part in result.parts:
            if part.mask & masks:
                raise Ambiguous("overlapping computed fields")
            masks |= part.mask
        return result

    def take(self, shift: int, width: int) -> str:
        mask = ((1 << width) - 1) << shift
        self.used |= mask
        pieces = [part for part in self.parts if part.mask & mask]
        if any(part.shift != shift or part.width != width for part in pieces):
            raise Ambiguous("operand can spill across parameter boundaries")
        value = (self.constant & mask) >> shift
        if not pieces:
            return str(value)
        operand = pieces[0].value
        return f"({operand}) | {value}" if value else operand

    def fixed(self, shift: int, width: int) -> int:
        value = integer(self.take(shift, width))
        if value is None:
            raise Ambiguous("computed command selector")
        return value

    def finish(self) -> None:
        if (self.constant | sum(part.mask for part in self.parts)) & ~self.used:
            raise Ambiguous("nonzero reserved bits")


def number(value: str, symbols: dict[int, str]) -> str:
    fixed = integer(value)
    return symbols.get(fixed, value) if fixed is not None else value


def plus_one(value: str) -> str:
    fixed = integer(value)
    if fixed is not None:
        return str(fixed + 1)
    match = re.fullmatch(r"(.+)\s*-\s*1", unwrap(value))
    return unwrap(match[1]) if match else f"({value}) + 1"
