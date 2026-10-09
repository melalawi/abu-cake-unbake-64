"""Recover resource-defined graphics macros from word assignment pairs."""

import re

from unbake import config
from unbake.contracts import Json

_NUMBER = r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)(?:[uU](?:[lL]{1,2})?|[lL]{1,2}[uU]?)?"
_OPAQUE = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
_POINTER = r"[A-Za-z_]\w*(?:\s*\[[^;{}]+?\]|\s*(?:->|\.)\s*[A-Za-z_]\w*)*?"
_ASSIGN = re.compile(rf"\s*({_POINTER})\s*(?:->|\.)\s*words\s*\.\s*w([01])\s*=\s*(.+);\s*", re.S)


def _strip(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for _index, char in enumerate(text):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                break
        if _index != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


def _integer(text: str) -> int | None:
    text = _strip(text)
    if not re.fullmatch(_NUMBER, text):
        return None
    text = re.sub(r"[uUlL]+$", "", text)
    base = (
        16
        if text.lower().startswith("0x")
        else 2
        if text.lower().startswith("0b")
        else 8
        if text.startswith("0")
        else 10
    )
    return int(text, base)


def _word(text: str) -> tuple[int, list[tuple[str, int, int]]] | None:
    text = _strip(text)
    parts, start, depth = [], 0, 0
    for index, char in enumerate(text):
        depth += (char in "([") - (char in ")]")
        if depth < 0:
            return None
        if char == "|" and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    if depth:
        return None
    parts.append(text[start:])
    literal, terms = 0, []
    for part in parts:
        part = _strip(part)
        value = _integer(part)
        if value is not None:
            literal |= value
            continue
        match = re.fullmatch(rf"\((.+)\s*&\s*({_NUMBER})\s*\)\s*<<\s*({_NUMBER})", part, re.S)
        if not match:
            match = re.fullmatch(rf"(.+)\s*&\s*({_NUMBER})", part, re.S)
            if match:
                expression, mask = match.groups()
                shift = "0"
            else:
                expression, mask, shift = part, "0xFFFFFFFF", "0"
        else:
            expression, mask, shift = match.groups()
        mask, shift = _integer(mask), _integer(shift)
        if (
            mask is None
            or shift is None
            or mask <= 0
            or mask & (mask + 1)
            or shift > 31
            or mask << shift > 0xFFFFFFFF
            or re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", expression)
        ):
            return None
        if any((m << s) & (mask << shift) for _, m, s in terms) or literal & (mask << shift):
            return None
        terms.append((_strip(expression), mask, shift))
    return (literal, terms) if literal <= 0xFFFFFFFF else None


def _format(value: int) -> str:
    return hex(value) if value > 9 else str(value)


def _value(field: Json, value: str, previous: dict[str, str]) -> str | None:
    number = _integer(value)
    xor, divisor, bias, scale = (
        field.get(k, default) for k, default in (("xor", 0), ("divisor", 1), ("bias", 0), ("scale", 1))
    )
    if "match" in field:
        return "" if number == field["match"] else None
    if number is not None:
        number ^= xor
        if number % divisor:
            return None
        number = (number // divisor + bias) * scale
        if "reverse" in field:
            number = field["reverse"] - number
        if "subtract" in field:
            other = _integer(previous[field["subtract"]])
            if other is None or number < other:
                return None
            number -= other
        choices = field.get("choices", {})
        if choices:
            return choices.get(str(number), None if field.get("strict") else _format(number))
        flags = field.get("flags", {})
        if flags:
            remainder = number & ~sum(int(bit) for bit in flags)
            return (
                " | ".join(
                    [name for bit, name in flags.items() if number & int(bit)]
                    + ([_format(remainder)] if remainder else [])
                )
                or "0"
            )
        return _format(number)
    if field.get("strict") or "reverse" in field or "subtract" in field:
        return None
    if divisor != 1:
        product = re.fullmatch(rf"(.+)\s*\*\s*({_NUMBER})", _strip(value), re.S)
        if product is None or _integer(product[2]) != divisor:
            return None
        value, divisor = _strip(product[1]), 1
    for op, amount in (
        ("^", xor),
        ("/", divisor if divisor != 1 else 0),
        ("+", bias),
        ("*", scale if scale != 1 else 0),
    ):
        if amount:
            value = f"({value}) {op} {_format(amount)}"
    return value


def _decode(row: Json, literal: int, terms: list[tuple[str, int, int]], word1: str) -> list[str] | None:
    low = _word(word1)
    if low is None:
        fields = [f for f in row["fields"] if f["word"] == 1 and "constant" not in f]
        pure = not re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", word1)
        if len(fields) != 1 or (fields[0]["shift"], fields[0]["bits"]) != (0, 32) or not pure:
            return None
        low = (0, [(_strip(word1), 0xFFFFFFFF, 0)])
    words, covered, used, values = [(literal, terms), low], [0xFF000000, 0], [set(), set()], {}
    for field in row["fields"]:
        if "constant" in field:
            values[field["name"]] = field["constant"]
            continue
        word, shift, bits = field["word"], field["shift"], field["bits"]
        mask = (1 << bits) - 1
        if shift + bits > 32 or covered[word] & (mask << shift):
            return None
        covered[word] |= mask << shift
        constant, computed = words[word]
        matches = [i for i, term in enumerate(computed) if term[1:] == (mask, shift)]
        if len(matches) > 1:
            return None
        if matches:
            index = matches[0]
            if index in used[word] or constant & (mask << shift):
                return None
            used[word].add(index)
            value = computed[index][0]
        else:
            value = _format((constant >> shift) & mask)
        value = _value(field, value, values)
        if value is None:
            return None
        if "match" not in field:
            values[field["name"]] = value
    if any(words[w][0] & ~covered[w] or len(used[w]) != len(words[w][1]) for w in (0, 1)):
        return None
    if row["macro"] == "gSPSetOtherMode":
        shift, length = _integer(values["shift"]), _integer(values["length"])
        if shift is None or length is None or shift < 0 or length < 1 or shift + length > 32:
            return None
    return [values[name] for name in row.get("args", values)]


def rewrite(text: str) -> tuple[str, int]:
    """Replace decodable pairs, preserving all other source text."""
    encodings = {}
    variant = next((name for name, macro in (("f3dex2", "F3DEX_GBI_2"), ("f3dex", "F3DEX_GBI"), ("f3d", "F3D_GBI"))
                    if re.search(rf"^\s*#\s*define\s+{macro}\b", text, re.M)), None)
    for row in config.load_resource("gbi.toml")["command"]:
        variants = row.get("variants", [{"opcode": row["opcode"], "microcode": row.get("microcode")}])
        for encoding in variants:
            encodings.setdefault(encoding["opcode"], []).append((row, encoding["microcode"]))
    masked = _OPAQUE.sub(lambda match: "".join("\n" if c == "\n" else " " for c in match[0]), text)
    statements, start, parens, braces = [], 0, 0, 0
    for boundary in re.finditer(r"[;{}()]", masked):
        end = boundary.end()
        char = boundary[0]
        if char in "()":
            parens += (char == "(") - (char == ")")
            continue
        if parens:
            continue
        if char == "{" and (
            braces or re.match(rf"\s*{_POINTER}\s*(?:->|\.)\s*words\s*\.\s*w[01]\s*=", masked[start:end])
        ):
            braces += 1
            continue
        if braces:
            braces -= char == "}"
            continue
        if boundary[0] == ";":
            match = _ASSIGN.fullmatch(masked[start:end])
            statements.append((start, end, match))
        else:
            statements.append((start, end, None))
        start = end
    if variant is None:
        evidence = set()
        for start, _end, match in statements:
            if match is None or match[2] != "0":
                continue
            word = _word(text[start + match.start(3) : start + match.end(3)])
            if word is not None:
                microcodes = {microcode for _row, microcode in encodings.get(word[0] >> 24, ())}
                if len(microcodes) == 1 and None not in microcodes:
                    evidence.update(microcodes)
        if len(evidence) == 1:
            variant = evidence.pop()
    opcodes = {
        opcode: [row for row, microcode in rows if variant is None or microcode in (None, variant)]
        for opcode, rows in encodings.items()
    }
    edits, consumed, count = [], set(), 0
    for index, (start, end, match) in enumerate(statements):
        if index in consumed or match is None or match[2] != "0":
            continue
        word = _word(text[match.start(3) + start : match.end(3) + start].strip())
        if word is None:
            continue
        literal, terms = word
        pointer = re.sub(r"\s+", "", match[1])
        if re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", pointer):
            continue
        for other in range(index + 1, min(index + 4, len(statements))):
            second_start, second_end, second = statements[other]
            if masked[second_end - 1] in "{}":
                break
            if second is None or re.sub(r"\s+", "", second[1]) != pointer:
                continue
            if second[2] == "0" or other in consumed:
                break
            operand = text[second_start + second.start(3) : second_start + second.end(3)].strip()
            candidates = []
            for row in opcodes.get(literal >> 24, ()):
                values = _decode(row, literal, terms, operand)
                if values is not None:
                    priority = sum("match" in f for f in row["fields"])
                    candidates.append((priority, f"{row['macro']}({', '.join([match[1].strip(), *values])});"))
            if candidates:
                priority = max(p for p, _ in candidates)
                choices = {c for p, c in candidates if p == priority}
                if len(choices) == 1:
                    edits.extend(
                        ((start + match.start(1), end, choices.pop()), (second_start + second.start(1), second_end, ""))
                    )
                    consumed.add(other)
                    count += 1
            break
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, count
