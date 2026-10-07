"""Measure generated scalar/void accesses without assigning a pointee layout.

Entry arguments survive only through audited word spills. Register provenance
stops at labels, calls and jumps; conditional fallthrough retains its facts.
A loaded value never inherits its address. C and
instruction order are deliberately unrelated: conflicting views require an
access offset or a measured stored value to distinguish them.
"""

import re
from dataclasses import dataclass

from unbake.cdecl import SOURCE_TOKEN, declaration_source
from unbake.config import Held
from unbake.decomp.draft_asm import _TRANSFER
from unbake.decomp.measured_access import _NO_RESULT, _VIEWS, _register
from unbake.layout.structs_types import SCALARS
from unbake.process import named as cause_named

_NUMBER = r"[+-]?(?:0[xX][\da-fA-F]+|\d+)"
_RELOCATION = re.compile(r"%([hl]i|lo)\(([A-Za-z_]\w*)(?:\s*([+-])\s*(0[xX][\da-fA-F]+|\d+))?\)")


@dataclass(frozen=True)
class Origin:
    name: str
    offset: int = 0
    indexed: bool = False
    complete: bool = True


@dataclass(frozen=True)
class Access:
    address: Origin
    spelling: str
    store: bool
    value: Origin | None


@dataclass(frozen=True)
class Parameter:
    name: str
    spelling: str


_AMBIGUOUS = Origin("?")


def instructions(assembly: str) -> list[tuple[str, list[str]]]:
    """One parse of the supplied function, including numeric register names."""
    clean = re.sub(r"/\*.*?\*/|#[^\n]*|//[^\n]*", "", assembly, flags=re.S)
    rows: list[tuple[str, list[str]]] = []
    for line in clean.splitlines():
        line = line.strip()
        if line.endswith(":") or re.match(r"(?:glabel|alabel|endlabel)\b", line):
            rows.append(("label", []))
        else:
            match = re.fullmatch(r"([A-Za-z][\w.]*)\s*(.*)", line)
            if match and not line.startswith("."):
                rows.append((match[1], [s.strip() for s in match[2].split(",")]))
    return rows


def _immediate(text: str) -> int | None:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return int(text, 0) if re.fullmatch(_NUMBER, text) else None


def _memory(args: list[str]) -> tuple[int, str] | None:
    if len(args) != 2:
        return None
    match = re.fullmatch(r"(" + _NUMBER + r")\((\$\w+)\)", args[1])
    return (int(match[1], 0), _register(match[2])) if match else None


def measurements(assembly: str, parameters: list[Parameter]) -> tuple[list[Access], set[tuple[str, int]]]:
    rows = instructions(assembly)
    # Establish a fixed entry-SP displacement and prologue spills, then audit
    # every direct write to incoming slots before reusing them at block edges.
    frame = 0
    spills: dict[int, Origin] = {}
    prologue = True
    entry = {f"a{i}": Origin(f"arg:{i}") for i in range(min(4, len(parameters)))}
    invalid: set[int] = set()
    for opcode, args in rows:
        if _TRANSFER.fullmatch(opcode):
            prologue = False
        destination = _register(args[0]) if args else ""
        if destination == "sp" and opcode not in _NO_RESULT and not _TRANSFER.fullmatch(opcode):
            if opcode == "addiu" and len(args) == 3 and _register(args[1]) == "sp":
                delta = _immediate(args[2])
                if prologue and frame == 0 and delta is not None and delta <= 0:
                    frame = delta
                elif delta != -frame:
                    return [], set()
            else:
                return [], set()
        memory = _memory(args)
        if opcode.startswith("s") and opcode in _NO_RESULT and memory and memory[1] == "sp":
            slot = memory[0] + frame
            if slot < 0:
                continue
            value = entry.get(destination)
            if prologue and opcode == "sw" and value and slot == int(value.name[4:]) * 4:
                spills[slot] = value
            else:
                invalid.add(slot // 4 * 4)
        elif prologue and destination and opcode not in _NO_RESULT:
            if (opcode == "move" and len(args) == 2) or (
                opcode in ("addu", "or") and len(args) == 3 and _register(args[2]) == "zero"
            ):
                value = entry.get(_register(args[1]))
            else:
                value = None
            entry.pop(destination, None)
            if value:
                entry[destination] = value
    spills = {slot: value for slot, value in spills.items() if slot not in invalid}
    state = {f"a{i}": Origin(f"arg:{i}") for i in range(min(4, len(parameters)))}
    state["zero"] = Origin("")
    delay = False
    seen_instruction = False
    accesses: list[Access] = []
    offsets: set[tuple[str, int]] = set()
    for opcode, args in rows:
        if opcode == "label":
            if seen_instruction:
                state = {"zero": Origin("")}
            continue
        seen_instruction = True
        destination = _register(args[0]) if args else ""
        value = None
        memory = _memory(args)
        if opcode in _VIEWS and memory:
            offset, register = memory
            if register == "sp":
                slot = offset + frame
                index = slot // 4
                if opcode.startswith("l") and 0 <= index < len(parameters) and slot // 4 * 4 not in invalid:
                    spelling = parameters[index].spelling
                    width = 4 if "*" in spelling else SCALARS.get(spelling, (0, 0))[0]
                    # Big-endian o32 narrow stack arguments are right justified.
                    if SCALARS[_VIEWS[opcode]][0] == width and slot % 4 == 4 - width:
                        value = spills.get(slot) if index < 4 else Origin(f"arg:{index}")
            else:
                base = state.get(register)
                if base and base.name.startswith("arg:") and base.complete:
                    address = Origin(base.name, base.offset + offset, base.indexed)
                    accesses.append(Access(address, _VIEWS[opcode], opcode.startswith("s"), state.get(destination)))
        elif opcode == "lui" and len(args) == 2:
            relocation = _RELOCATION.fullmatch(args[1])
            if relocation and relocation[1] == "hi":
                value = Origin("global:" + relocation[2], complete=False)
        elif opcode in ("addi", "addiu") and len(args) == 3:
            base = state.get(_register(args[1]))
            number = _immediate(args[2])
            relocation = _RELOCATION.fullmatch(args[2])
            if base and relocation and relocation[1] == "lo":
                if base.name == "global:" + relocation[2] and not base.complete:
                    delta = int(relocation[4], 0) if relocation[4] else 0
                    value = Origin(base.name, -delta if relocation[3] == "-" else delta)
            elif base and base.complete and number is not None:
                value = Origin(base.name, base.offset + number, base.indexed)
        elif opcode in ("add", "addu", "sub", "subu", "or") and len(args) == 3:
            left, right = (state.get(_register(arg)) for arg in args[1:])
            if opcode == "or":
                value = left if right == Origin("") else right if left == Origin("") else None
            elif left and right and left.name and right.name:
                value = _AMBIGUOUS
            elif left and left.complete:
                if right is None:
                    value = Origin(left.name, left.offset, True)
                else:
                    value = Origin(
                        left.name,
                        left.offset + (-right.offset if opcode.startswith("sub") else right.offset),
                        left.indexed or right.indexed,
                    )
            elif right and right.complete and opcode in ("add", "addu"):
                value = Origin(right.name, right.offset, True) if left is None else right
        elif opcode == "move" and len(args) == 2:
            value = state.get(_register(args[1]))
        transfer = bool(_TRANSFER.fullmatch(opcode))
        if destination and destination != "zero" and not transfer and opcode not in _NO_RESULT:
            if (
                value is None
                and opcode not in _VIEWS
                and any(state.get(_register(arg), Origin("")).name for arg in args[1:])
            ):
                value = _AMBIGUOUS
            state.pop(destination, None)
            if value:
                state[destination] = value
                if value.name.startswith("global:") and value.complete and not value.indexed:
                    offsets.add((value.name[7:], value.offset))
        if delay:
            state = {"zero": Origin("")}
        # A conditional branch's fallthrough executes this same register
        # state. A target label gets no such fact; calls/jumps kill after
        # their delay slot, which still observes the pre-transfer state.
        conditional = opcode.startswith("b") and opcode not in ("b", "bal") and not opcode.endswith(("al", "all"))
        delay = transfer and not conditional
    return accesses, offsets


def _parameters(source: str, function: str) -> tuple[list[Parameter], int]:
    match = re.search(r"\b" + re.escape(function) + r"\s*\(([^{};]*)\)\s*\{", source)
    if match is None:
        return [], len(source)
    parameters = []
    for argument in match[1].split(","):
        argument = argument.strip()
        if argument == "void" or not argument:
            continue
        declaration = re.fullmatch(r"(.+?)[\s*]([A-Za-z_]\w*)", argument)
        if declaration is None:
            return [], match.end()
        name = declaration[2]
        spelling = argument[: argument.rfind(name)].strip()
        # Multiword/FP ABI placement is a separate prerequisite. Never map
        # those signatures to invented integer argument slots.
        width = 4 if "*" in spelling else SCALARS.get(spelling, (0, 0))[0]
        if width not in (1, 2, 4) or spelling in ("float", "f32"):
            return [], match.end()
        parameters.append(Parameter(name, spelling))
    return parameters, match.end()


def _displacement(tail: str) -> tuple[int, bool] | None:
    if not tail.strip():
        return 0, False
    # Split only top-level additive terms: multipliers inside an index remain
    # verbatim byte displacements, never a change to an element stride.
    depth = 0
    starts = []
    for index, char in enumerate(tail):
        depth += (char == "(") - (char == ")")
        if depth == 0 and char in "+-":
            starts.append(index)
    if not starts or tail[: starts[0]].strip():
        return None
    offset = 0
    indexed = False
    for start, end in zip(starts, [*starts[1:], len(tail)], strict=True):
        term = _immediate(tail[start + 1 : end])
        if term is None:
            indexed = True
        else:
            offset += term * (-1 if tail[start] == "-" else 1)
    return offset, indexed


def _stored_value(text: str, parameters: list[Parameter]) -> Origin | None:
    text = re.sub(r"^\s*(?:\((?:[su]\d+|unsigned char|signed char|short|unsigned short|int)\)\s*)+", "", text).strip()
    number = _immediate(text)
    if number is not None:
        return Origin("", number)
    for index, parameter in enumerate(parameters):
        if text == parameter.name:
            return Origin(f"arg:{index}")
    return None


def lower(function: str, source: str, assembly: str) -> str:
    clean = declaration_source(source)
    parameters, body = _parameters(clean, function)
    transports = {
        parameter.name: index
        for index, parameter in enumerate(parameters)
        if "*" not in parameter.spelling or re.fullmatch(r"(?:const\s+)?(?:void|M2C_UNK\d*)\s*\*", parameter.spelling)
    }
    tokens = list(SOURCE_TOKEN.finditer(clean))
    candidates: list[tuple[int, int, str, str, Origin]] = []
    byte_candidates: list[tuple[int, int, str, int]] = []
    for index, token in enumerate(tokens):
        if token.start() < body or token[0].startswith(('"', "'")):
            continue
        if token[0] == "&" and index + 3 < len(tokens) and tokens[index + 2][0] in ("+", "-"):
            match = re.match(r"&([A-Za-z_]\w*)\s*([+-])\s*(0[xX][\da-fA-F]+|\d+)\b", clean[token.start() :])
            previous = tokens[index - 1][0] if index else ""
            if match and previous in ("(", "=", ",", "return", "+", "-"):
                delta = int(match[3], 0) * (-1 if match[2] == "-" else 1)
                byte_candidates.append((token.start(), tokens[index + 1].end(), match[1], delta))
        if token[0] != "*" or index + 1 >= len(tokens):
            continue
        previous = tokens[index - 1][0] if index else ""
        if previous not in ("(", "=", ",", "return", "+", "-", "!", "{", ";", "}", ":", "&", "|"):
            continue
        head = index + 1
        tail = head
        if tokens[head][0] == "(":
            depth = 1
            tail += 1
            while tail < len(tokens):
                depth += (tokens[tail][0] == "(") - (tokens[tail][0] == ")")
                if depth == 0:
                    break
                tail += 1
            if depth:
                raise Held(
                    cause_named(
                        f"{function}",
                        f"{function}: unclosed memory operand",
                        owner="decomp.measured_memory",
                        stage="m2c",
                    )
                )
            operand = source[tokens[head].end() : tokens[tail].start()].strip()
        else:
            operand = tokens[head][0]
        match = re.fullmatch(r"([A-Za-z_]\w*)(.*)", operand, re.S)
        if match is None or match[1] not in transports:
            continue
        displacement = _displacement(match[2])
        if displacement is None:
            raise Held(
                cause_named(
                    f"{function}.{match[1]}",
                    f"{function}.{match[1]}: unsupported non-affine memory address",
                    owner="decomp.measured_memory",
                    stage="m2c",
                )
            )
        address = Origin(f"arg:{transports[match[1]]}", *displacement)
        candidates.append((token.start(), tokens[tail].end(), match[1], operand, address))
    if not candidates and not byte_candidates:
        return source
    accesses, byte_offsets = measurements(assembly, parameters)
    edits = []
    for start, end, name, operand, address in candidates:
        after = clean[end:].lstrip()
        store = bool(re.match(r"=(?!=)", after))
        compound = bool(
            re.match(r"(?:[+*/%&|^-]|<<|>>)=|\+\+|--", after) or re.search(r"(?:\+\+|--)\s*$", clean[:start])
        )
        rows = [a for a in accesses if a.address == address and a.store == store]
        if store:
            assignment = re.match(r"=\s*([^;]+);", after)
            stored = _stored_value(assignment[1], parameters) if assignment else None
            if stored is not None:
                # An unknown stored value can still be this RHS. Keep its
                # view as a candidate rather than using order to choose.
                rows = [a for a in rows if a.value is None or a.value == stored]
        choices = {a.spelling for a in rows}
        if len(choices) != 1:
            raise Held(
                cause_named(
                    f"{function}.{name}",
                    f"{function}.{name}: memory {('store' if store else 'load')} lacks a unique measured view",
                    owner="decomp.measured_memory",
                    stage="m2c",
                )
            )
        spelling = next(iter(choices))
        if compound and {SCALARS[a.spelling][0] for a in accesses if a.address == address and a.store} != {
            SCALARS[spelling][0]
        }:
            raise Held(
                cause_named(
                    f"{function}.{name}",
                    f"{function}.{name}: read/modify/write lacks a measured width",
                    owner="decomp.measured_memory",
                    stage="m2c",
                )
            )
        # Cast the base before adding displacement; casting the completed
        # pointer expression would leave C's element scaling in place.
        byte_address = operand.replace(name, f"(unsigned char *){name}", 1)
        edits.append((start, end, f"*(({spelling} *)({byte_address}))"))
    for start, end, name, delta in byte_candidates:
        if (name, delta) in byte_offsets:
            edits.append((start, end, f"(unsigned char *)&{name}"))
        else:
            declaration = re.search(r"\bextern\s+([^;]+?)\s+" + re.escape(name) + r"\s*;", clean)
            if declaration and SCALARS.get(declaration[1].strip(), (0, 0))[0] > 1:
                raise Held(
                    cause_named(
                        f"{function}.{name}",
                        f"{function}.{name}: byte address lacks a measured displacement",
                        owner="decomp.measured_memory",
                        stage="m2c",
                    )
                )
    edits.sort(reverse=True)
    following = len(source)
    for start, end, value in edits:
        if end > following:
            raise Held(
                cause_named(
                    f"{function}",
                    f"{function}: overlapping memory operands need separate provenance",
                    owner="decomp.measured_memory",
                    stage="m2c",
                )
            )
        source = source[:start] + value + source[end:]
        following = start
    return source
