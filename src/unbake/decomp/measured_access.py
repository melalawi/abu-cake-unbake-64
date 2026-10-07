"""MIPS access views from symbolic relocations and local affine addresses.

Only access width and extension are measured here. An indexed address gives
neither an extent nor a semantic pointee type. Provenance stops at block edges;
loads consume an address, but their result does not inherit that address.
"""

import re
from dataclasses import dataclass

from unbake.decomp.draft_asm import _TRANSFER

_VIEWS = {
    "lh": "short",
    "lhu": "unsigned short",
    "lw": "int",
    "lb": "signed char",
    "lbu": "unsigned char",
    "sh": "unsigned short",
    "sw": "int",
    "sb": "unsigned char",
}
_REGISTERS = (
    "zero",
    "at",
    "v0",
    "v1",
    "a0",
    "a1",
    "a2",
    "a3",
    "t0",
    "t1",
    "t2",
    "t3",
    "t4",
    "t5",
    "t6",
    "t7",
    "s0",
    "s1",
    "s2",
    "s3",
    "s4",
    "s5",
    "s6",
    "s7",
    "t8",
    "t9",
    "k0",
    "k1",
    "gp",
    "sp",
    "fp",
    "ra",
)
_EXPRESSION = r"([A-Za-z_]\w*)(?:\s*[+-]\s*(?:0[xX][\da-fA-F]+|\d+))?"
_NUMBER = r"[+-]?(?:0[xX][\da-fA-F]+|\d+)"
_NO_RESULT = frozenset(
    (
        "sb",
        "sh",
        "sw",
        "swl",
        "swr",
        "sd",
        "sdl",
        "sdr",
        "swc1",
        "sdc1",
        "mult",
        "multu",
        "div",
        "divu",
        "mthi",
        "mtlo",
        "mtc0",
        "mtc1",
        "ctc1",
    )
)


def _register(text: str) -> str:
    if not text.startswith("$"):
        return ""
    name = text[1:]
    if name.isdecimal() and int(name) < len(_REGISTERS):
        name = _REGISTERS[int(name)]
    return "fp" if name == "s8" else name


@dataclass(frozen=True)
class _Base:
    symbol: str
    complete: bool


_AMBIGUOUS = _Base("", False)


def views(assembly: str) -> dict[str, tuple[set[str], set[str]]]:
    """Collect reads/writes once, without associating instruction order with C order.

    A high relocation needs its matching low relocation before numeric memory
    operands can name the global. Addition preserves one base and arbitrary
    byte displacement, never a sum of bases or a scaled/nonlinear base.
    """
    result: dict[str, tuple[set[str], set[str]]] = {}
    state: dict[str, _Base] = {}
    clean = re.sub(r"/\*.*?\*/|\#[^\n]*|//[^\n]*", lambda m: "\n" * m[0].count("\n"), assembly, flags=re.S)
    delay = False
    for line in clean.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith(":") or re.match(r"(?:glabel|alabel|endlabel)\b", line):
            state.clear()
            continue
        instruction = re.fullmatch(r"([A-Za-z][\w.]*)\s*(.*)", line)
        if instruction is None:
            continue
        opcode, operands = instruction.groups()
        args = [arg.strip() for arg in operands.split(",")]
        destination = _register(args[0])
        next_base = None
        if opcode in _VIEWS and len(args) == 2:
            memory = re.fullmatch(r"(.+)\((\$\w+)\)", args[1])
            if memory:
                offset, register = memory.groups()
                base = state.get(_register(register))
                low = re.fullmatch(r"%lo\(" + _EXPRESSION + r"\)", offset)
                symbol = ""
                if low and (base is None or (base.symbol == low[1] and not base.complete)):
                    # Keep standalone symbolic operands as measured evidence,
                    # as before; reject a known conflicting/ambiguous base.
                    symbol = low[1]
                elif re.fullmatch(_NUMBER, offset) and base and base.complete:
                    symbol = base.symbol
                if symbol:
                    reads, writes = result.setdefault(symbol, (set(), set()))
                    (writes if opcode.startswith("s") else reads).add(_VIEWS[opcode])
        elif opcode == "lui" and len(args) == 2:
            high = re.fullmatch(r"%hi\(" + _EXPRESSION + r"\)", args[1])
            if high:
                next_base = _Base(high[1], False)
        elif opcode == "la" and len(args) == 2:
            address = re.fullmatch(_EXPRESSION, args[1])
            if address:
                next_base = _Base(address[1], True)
        elif opcode in ("addi", "addiu") and len(args) == 3:
            base = state.get(_register(args[1]))
            low = re.fullmatch(r"%lo\(" + _EXPRESSION + r"\)", args[2])
            if low and base:
                next_base = _Base(low[1], True) if base.symbol == low[1] and not base.complete else _AMBIGUOUS
            elif re.fullmatch(_NUMBER, args[2]):
                next_base = base
        elif opcode in ("add", "addu", "sub", "subu") and len(args) == 3:
            left, right = (state.get(_register(arg)) for arg in args[1:])
            if left and right:
                next_base = _AMBIGUOUS
            elif opcode in ("add", "addu"):
                next_base = left or right
            elif left:
                next_base = left
            elif right:
                next_base = _AMBIGUOUS
        elif opcode == "move" and len(args) == 2:
            next_base = state.get(_register(args[1]))
        elif opcode == "or" and len(args) == 3:
            if _register(args[1]) == "zero":
                next_base = state.get(_register(args[2]))
            elif _register(args[2]) == "zero":
                next_base = state.get(_register(args[1]))
        # Stores and transfers have no first-operand GPR result. Other
        # instructions kill it, including loads into the address register.
        transfer = bool(_TRANSFER.fullmatch(opcode))
        if destination in _REGISTERS and destination != "zero" and not transfer and opcode not in _NO_RESULT:
            if next_base is None and not opcode.startswith("l") and any(_register(arg) in state for arg in args[1:]):
                # Scaling or masking a symbolic base is no longer an affine
                # address. Keep that ambiguity through later arithmetic.
                next_base = _AMBIGUOUS
            if next_base is None:
                state.pop(destination, None)
            else:
                state[destination] = next_base
        if delay:
            state.clear()
        delay = transfer
    return result
