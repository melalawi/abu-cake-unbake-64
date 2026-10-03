"""Source boundaries and transparent, file-local command builders."""

from __future__ import annotations

import re
from dataclasses import dataclass

from unbake.decomp.gbi_expr import split, unwrap

DEFINE = re.compile(r"^[ \t]*#\s*define\s+(\w+)\(([^\n)]*)\)[ \t]*((?:[^\n]*\\\n)*[^\n]*)", re.M)


@dataclass
class Macro:
    name: str
    parameters: list[str]
    body: str
    start: int
    end: int

    def substitute(self, arguments: list[str]) -> str | None:
        if len(arguments) != len(self.parameters) or "#" in self.body:
            return None
        replacements = dict(zip(self.parameters, arguments, strict=True))
        return re.sub(
            r"\b\w+\b",
            lambda match: "(" + replacements[match[0]] + ")" if match[0] in replacements else match[0],
            self.body,
        )


def macros(source: str) -> dict[str, Macro]:
    result: dict[str, Macro] = {}
    conflicts: set[str] = set()
    for match in DEFINE.finditer(source):
        if match[1] in result and result[match[1]].body != match[3].replace("\\\n", " "):
            conflicts.add(match[1])
        result[match[1]] = Macro(
            match[1],
            split(match[2], ",") if match[2].strip() else [],
            match[3].replace("\\\n", " "),
            match.start(),
            match.end(),
        )
    return {name: macro for name, macro in result.items() if name not in conflicts}


def invocations(source: str, name: str) -> list[tuple[int, int, list[str]]]:
    result = []
    cursor = 0
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    while match := pattern.search(source, cursor):
        depth = 1
        i = match.end()
        begin = i
        while i < len(source) and depth:
            depth += (source[i] == "(") - (source[i] == ")")
            i += 1
        if depth:
            break
        result.append((match.start(), i, split(source[begin : i - 1], ",") if source[begin : i - 1].strip() else []))
        cursor = i
    return result


def expand(text: str, definitions: dict[str, Macro], depth: int = 0) -> str:
    if depth > 12:
        return text
    for name, macro in definitions.items():
        if name == "_SHIFTL" or ";" in macro.body or "#" in macro.body:
            continue
        for start, end, args in reversed(invocations(text, name)):
            body = macro.substitute(args)
            if body is not None:
                text = text[:start] + expand(body, definitions, depth + 1) + text[end:]
    return text


def constants(source: str) -> dict[str, str]:
    """Unconditional, integer-valued object macros only; never symbol aliases."""
    from unbake.decomp.gbi_expr import integer

    result = {}
    seen: set[str] = set()
    conflicts: set[str] = set()
    depth = 0
    for line in source.splitlines():
        if re.match(r"\s*#\s*(?:if|ifdef|ifndef)\b", line):
            depth += 1
        elif re.match(r"\s*#\s*endif\b", line):
            depth -= 1
        elif match := re.match(r"\s*#\s*(define|undef)\s+(\w+)(.*)$", line):
            name = match[2]
            if name in seen or depth or match[1] == "undef":
                conflicts.add(name)
            seen.add(name)
            if depth or match[1] == "undef" or not match[3].startswith((" ", "\t")):
                continue
            value = integer(re.sub(r"/\*.*?\*/|//.*", "", match[3]).strip())
            if value is not None:
                result[name] = str(value)
    return {name: value for name, value in result.items() if name not in conflicts}


def word_builder(macro: Macro, definitions: dict[str, Macro], packet_type: str = "Gfx") -> tuple[str, str, str] | None:
    body = macro.body
    # Expand aliases to a file-local pair builder, keeping packet evaluation.
    for name, other in definitions.items():
        if name == macro.name or not re.search(r"->\s*(?:words\.)?w0", other.body):
            continue
        calls = invocations(body, name)
        if len(calls) == 1:
            start, end, args = calls[0]
            replaced = other.substitute(args)
            if replaced is not None:
                body = body[:start] + replaced + body[end:]
    pointer = re.search(r"\b" + re.escape(packet_type) + r"\s*\*\s*(\w+)\s*=\s*([^;]+);", body)
    if not pointer:
        return None
    pair = re.search(
        re.escape(pointer[1])
        + r"\s*->\s*(?:words\.)?w0\s*=(?!=)\s*([^;]+);\s*"
        + re.escape(pointer[1])
        + r"\s*->\s*(?:words\.)?w1\s*=(?!=)\s*([^;]+);",
        body,
    )
    if not pair:
        return None
    remaining = body[: pointer.start()] + body[pointer.end() : pair.start()] + body[pair.end() :]
    if re.sub(r"\s|[{}]|do|while\s*\(0\)", "", remaining):
        return None
    return unwrap(pointer[2]), pair[1].strip(), pair[2].strip()


def gfx_typedefs(source: str) -> list[tuple[int, int]]:
    result = []
    for match in re.finditer(r"\btypedef\s+(?:struct|union)\s*(?:[A-Za-z_]\w*\s*)?\{", source):
        depth, i = 1, match.end()
        while i < len(source) and depth:
            depth += (source[i] == "{") - (source[i] == "}")
            i += 1
        end = re.match(r"\s*Gfx\s*;", source[i:])
        if end:
            result.append((match.start(), i + end.end()))
    return result


def typedefs(source: str) -> dict[str, tuple[int, int, str]]:
    """Collect balanced typedef statements, including nested aggregate fields."""
    result = {}
    for match in re.finditer(r"\btypedef\b", source):
        depth, i = 0, match.end()
        while i < len(source):
            char = source[i]
            depth += (char == "{") - (char == "}")
            i += 1
            if char == ";" and depth == 0:
                break
        declaration = source[match.start() : i]
        name = re.search(r"\b(\w+)\s*(?:\[[^]]*\]\s*)*;\s*$", declaration)
        if name:
            result[name[1]] = (match.start(), i, declaration)
    return result


def tokens(source: str) -> list[str]:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    return re.findall(r"\w+|[^\s]", source)


def packet_aliases(source: str, packet_type: str) -> set[str]:
    """Resolve transitive, non-pointer aliases without treating other structs as packets."""
    names = {packet_type}
    aliases = re.findall(r"\btypedef\s+(\w+)\s+(\w+)\s*;", source)
    while True:
        updated = names | {alias for target, alias in aliases if target in names}
        if updated == names:
            return names
        names = updated


def packet_pointers(source: str, packet_type: str) -> set[str]:
    names = "|".join(re.escape(name) for name in sorted(packet_aliases(source, packet_type)))
    return set(re.findall(r"\b(?:" + names + r")\s+(?:const\s+|volatile\s+)?\*\s*(\w+)", source))


def initializer_pairs(source: str) -> list[tuple[int, int, str, str]]:
    """Locate explicit Gfx.words array elements; never infer other union members."""
    result = []
    names = "(?:" + "|".join(sorted(packet_aliases(source, "Gfx"))) + ")"
    for declaration in re.finditer(r"\b" + names + r"\s+[A-Za-z_]\w*\s*\[[^\]]*\]\s*=\s*\{", source):
        depth, cursor, begin = 1, declaration.end(), declaration.end()
        while cursor < len(source) and depth:
            char = source[cursor]
            depth += (char == "{") - (char == "}")
            if (char == "," and depth == 1) or depth == 0:
                element = source[begin:cursor]
                match = re.fullmatch(r"\s*(\{\s*\{(.*)\}\s*\})\s*", element, re.S)
                if match:
                    values = split(match[2], ",")
                    if len(values) == 2:
                        result.append((begin + match.start(1), begin + match.end(1), values[0], values[1]))
                begin = cursor + 1
            cursor += 1
    return result


def standard_shiftl(macro: Macro) -> bool:
    """Prove the local definition has the standard mask-before-shift shape."""
    from unbake.decomp.gbi_expr import integer, scalar

    if len(macro.parameters) != 3:
        return False
    value, shift, width = macro.parameters
    parts = split(scalar(macro.body), "<<")
    if len(parts) != 2 or unwrap(parts[1]) != shift:
        return False
    masked = split(unwrap(parts[0]), "&")
    if len(masked) != 2 or scalar(masked[0]) != value:
        return False
    mask = unwrap(masked[1])
    subtract = split(mask, "-")
    if len(subtract) == 2 and integer(subtract[1]) == 1:
        power = split(unwrap(subtract[0]), "<<")
        return len(power) == 2 and integer(power[0]) == 1 and unwrap(power[1]) == width
    right = split(mask, ">>")
    if len(right) == 2 and integer(right[0]) == 0xFFFFFFFF:
        complement = split(unwrap(right[1]), "-")
        return len(complement) == 2 and integer(complement[0]) == 32 and unwrap(complement[1]) == width
    return False
