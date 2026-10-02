"""Independent reconstruction and conservative lowering of standard Acmd packets."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.gbi_expr import Ambiguous, Word, pure, scalar, unwrap
from unbake.decomp.gbi_source import Macro, macros, tokens, word_builder

HEADER = Path(__file__).parents[1] / "project_tools/abi.h"
TYPE_HEADER = HEADER.with_name("acmd.h")


def signature(expression: str) -> tuple[int, tuple[tuple[int, int, tuple[str, ...]], ...]]:
    word = Word.parse(expression)
    return word.constant, tuple(sorted((p.shift, p.width, tuple(tokens(scalar(p.value)))) for p in word.parts))


def equivalent(macro: Macro, definitions: dict[str, Macro]) -> bool:
    """Compare symbolic encodings, not just builder names or concrete call inputs."""
    expected = macros(HEADER.read_text()).get(macro.name)
    builder = word_builder(macro, definitions, "Acmd")
    if expected is None or builder is None or len(expected.parameters) != len(macro.parameters):
        return False
    body = expected.substitute(macro.parameters)
    if body is None:
        return False
    template = Macro(expected.name, macro.parameters, body, 0, 0)
    reference = word_builder(template, {}, "Acmd")
    if reference is None:
        return False
    pointer = re.sub(r"^\(\s*Acmd\s*\*\s*\)\s*", "", unwrap(builder[0]))
    if unwrap(pointer) != macro.parameters[0]:
        return False
    try:
        return all(signature(a) == signature(b) for a, b in zip(builder[1:], reference[1:], strict=True))
    except Ambiguous:
        return False


def decode(w0: str, w1: str) -> tuple[str, list[str]]:
    hi, lo = Word.parse(w0), Word.parse(w1)
    opcode = hi.fixed(24, 8)
    names = {
        1: "aADPCMdec",
        2: "aClearBuffer",
        3: "aEnvMixer",
        4: "aLoadBuffer",
        5: "aResample",
        6: "aSaveBuffer",
        7: "aSegment",
        8: "aSetBuffer",
        9: "aSetVolume",
        10: "aDMEMMove",
        11: "aLoadADPCM",
        12: "aMix",
        13: "aInterleave",
        14: "aPoleFilter",
        15: "aSetLoop",
    }
    if opcode not in names:
        raise Ambiguous(f"unsupported audio opcode 0x{opcode:02X}")
    if opcode in (1, 3):
        args = [hi.take(16, 8), lo.take(0, 32)]
    elif opcode in (2, 11):
        args = [hi.take(0, 24), lo.take(0, 32)]
    elif opcode in (4, 6, 15):
        args = [lo.take(0, 32)]
    elif opcode in (5, 14):
        args = [hi.take(16, 8), hi.take(0, 16), lo.take(0, 32)]
    elif opcode == 7:
        args = [lo.take(24, 8), lo.take(0, 24)]
    elif opcode in (8, 9, 12):
        # The decoder only accepts an eight-bit flag. Definition equivalence
        # above separately preserves aSetVolume's legacy sixteen-bit mask.
        args = [hi.take(16, 8), hi.take(0, 16), lo.take(16, 16), lo.take(0, 16)]
    elif opcode == 10:
        args = [hi.take(0, 24), lo.take(16, 16), lo.take(0, 16)]
    else:
        args = [lo.take(16, 16), lo.take(0, 16)]
    hi.finish()
    lo.finish()
    if not all(pure(arg) for arg in args):
        raise Ambiguous("audio operand has evaluation effects")
    return names[opcode], args
