"""Recover standard GBI calls from proven word-pair encodings.

The same lowering is used for m2c drafts and existing C. Unknown encodings stay
verbatim. No symbol names or game-specific addresses participate in decoding.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from unbake.decomp.gbi_expr import Ambiguous, Word, integer, number, plus_one, pure, split, unwrap
from unbake.decomp.gbi_source import (
    expand,
    gfx_typedefs,
    initializer_pairs,
    invocations,
    macros,
    standard_shiftl,
    tokens,
    typedefs,
    word_builder,
)
from unbake.project.config import Held, Project

OtherOptions: TypeAlias = list[str] | dict[int, str]

HEADER = Path(__file__).parents[1] / "project_tools/gbi.h"
SYNC = {0xE6: "gDPLoadSync", 0xE7: "gDPPipeSync", 0xE8: "gDPTileSync", 0xE9: "gDPFullSync"}
FORMATS = {0: "G_IM_FMT_RGBA", 1: "G_IM_FMT_YUV", 2: "G_IM_FMT_CI", 3: "G_IM_FMT_IA", 4: "G_IM_FMT_I"}
SIZES = {0: "G_IM_SIZ_4b", 1: "G_IM_SIZ_8b", 2: "G_IM_SIZ_16b", 3: "G_IM_SIZ_32b"}
WRAPS = {0: "G_TX_WRAP", 1: "G_TX_MIRROR", 2: "G_TX_CLAMP", 3: "G_TX_MIRROR | G_TX_CLAMP"}
OTHER_H: dict[tuple[int, int], tuple[str, OtherOptions]] = {
    (4, 2): ("AlphaDither", ["G_AD_PATTERN", "G_AD_NOTPATTERN", "G_AD_NOISE", "G_AD_DISABLE"]),
    (6, 2): ("ColorDither", ["G_CD_MAGICSQ", "G_CD_BAYER", "G_CD_NOISE", "G_CD_DISABLE"]),
    (8, 1): ("CombineKey", ["G_CK_NONE", "G_CK_KEY"]),
    (9, 3): ("TextureConvert", {0: "G_TC_CONV", 5: "G_TC_FILTCONV", 6: "G_TC_FILT"}),
    (12, 2): ("TextureFilter", {0: "G_TF_POINT", 2: "G_TF_BILERP", 3: "G_TF_AVERAGE"}),
    (14, 2): ("TextureLUT", {0: "G_TT_NONE", 2: "G_TT_RGBA16", 3: "G_TT_IA16"}),
    (16, 1): ("TextureLOD", ["G_TL_TILE", "G_TL_LOD"]),
    (17, 2): ("TextureDetail", ["G_TD_CLAMP", "G_TD_SHARPEN", "G_TD_DETAIL"]),
    (19, 1): ("TexturePersp", ["G_TP_NONE", "G_TP_PERSP"]),
    (20, 2): ("CycleType", ["G_CYC_1CYCLE", "G_CYC_2CYCLE", "G_CYC_COPY", "G_CYC_FILL"]),
    (23, 1): ("PipelineMode", ["G_PM_NPRIMITIVE", "G_PM_1PRIMITIVE"]),
}
OP_NAMES = {
    **SYNC,
    0xE2: "G_SETOTHERMODE_L",
    0xE3: "G_SETOTHERMODE_H",
    0xE4: "G_TEXRECT",
    0xED: "G_SETSCISSOR",
    0xF0: "G_LOADTLUT",
    0xF2: "G_SETTILESIZE",
    0xF3: "G_LOADBLOCK",
    0xF4: "G_LOADTILE",
    0xF5: "G_SETTILE",
    0xF6: "G_FILLRECT",
    0xF7: "G_SETFILLCOLOR",
    0xF8: "G_SETFOGCOLOR",
    0xF9: "G_SETBLENDCOLOR",
    0xFA: "G_SETPRIMCOLOR",
    0xFB: "G_SETENVCOLOR",
    0xFC: "G_SETCOMBINE",
    0xFD: "G_SETTIMG",
    0xFE: "G_SETZIMG",
    0xFF: "G_SETCIMG",
    0xDA: "G_MTX",
    0xDB: "G_MOVEWORD",
    0xDC: "G_MOVEMEM",
    0xDE: "G_DL",
    0xDF: "G_ENDDL",
    0xD9: "G_GEOMETRYMODE",
    0xD7: "G_TEXTURE",
}


def flags(value: str, choices: dict[int, str]) -> str:
    fixed = integer(value)
    if fixed is None:
        return value
    names = [name for bit, name in choices.items() if fixed & bit]
    remainder = fixed & ~sum(choices)
    if remainder:
        names.append(f"0x{remainder:X}")
    return " | ".join(names) or "0"


def decode(w0: str, w1: str, variant: str | None) -> tuple[str, list[str]]:
    hi = Word.parse(w0)
    opcode = hi.fixed(24, 8)
    if opcode in SYNC:
        hi.finish()
        if integer(w1) != 0:
            raise Ambiguous("sync payload must be zero")
        return SYNC[opcode], []
    if opcode in (0xF7, 0xFE):
        hi.finish()
        return ("gDPSetFillColor" if opcode == 0xF7 else "gDPSetDepthImage"), [w1]
    if opcode in (0xFD, 0xFF):
        args = [number(hi.take(21, 3), FORMATS), number(hi.take(19, 2), SIZES), plus_one(hi.take(0, 12)), w1]
        name = "gDPSetTextureImage" if opcode == 0xFD else "gDPSetColorImage"
    elif opcode in (0xF8, 0xF9, 0xFA, 0xFB):
        lo = Word.parse(w1)
        args = [hi.take(8, 8), hi.take(0, 8)] if opcode == 0xFA else []
        args += [lo.take(shift, 8) for shift in (24, 16, 8, 0)]
        lo.finish()
        name = {0xF8: "gDPSetFogColor", 0xF9: "gDPSetBlendColor", 0xFA: "gDPSetPrimColor", 0xFB: "gDPSetEnvColor"}[
            opcode
        ]
    elif opcode == 0xF6:
        lo = Word.parse(w1)
        args = [lo.take(14, 10), lo.take(2, 10), hi.take(14, 10), hi.take(2, 10)]
        lo.finish()
        name = "gDPFillRectangle"
    elif opcode in (0xF2, 0xF3, 0xF4):
        lo = Word.parse(w1)
        args = [
            number(lo.take(24, 3), {7: "G_TX_LOADTILE", 0: "G_TX_RENDERTILE"}),
            hi.take(12, 12),
            hi.take(0, 12),
            lo.take(12, 12),
            lo.take(0, 12),
        ]
        lo.finish()
        name = {0xF2: "gDPSetTileSize", 0xF3: "gDPLoadBlock", 0xF4: "gDPLoadTile"}[opcode]
    elif opcode == 0xF5:
        lo = Word.parse(w1)
        args = [
            number(hi.take(21, 3), FORMATS),
            number(hi.take(19, 2), SIZES),
            hi.take(9, 9),
            hi.take(0, 9),
            number(lo.take(24, 3), {7: "G_TX_LOADTILE", 0: "G_TX_RENDERTILE"}),
            lo.take(20, 4),
            number(lo.take(18, 2), WRAPS),
            lo.take(14, 4),
            lo.take(10, 4),
            number(lo.take(8, 2), WRAPS),
            lo.take(4, 4),
            lo.take(0, 4),
        ]
        lo.finish()
        name = "gDPSetTile"
    elif opcode == 0xF0:
        lo = Word.parse(w1)
        args = [number(lo.take(24, 3), {7: "G_TX_LOADTILE", 0: "G_TX_RENDERTILE"}), lo.take(14, 10)]
        lo.finish()
        name = "gDPLoadTLUTCmd"
    elif opcode == 0xED:
        lo = Word.parse(w1)
        args = [
            number(lo.take(24, 2), {0: "G_SC_NON_INTERLACE", 2: "G_SC_EVEN_INTERLACE", 3: "G_SC_ODD_INTERLACE"}),
            hi.take(12, 12),
            hi.take(0, 12),
            lo.take(12, 12),
            lo.take(0, 12),
        ]
        lo.finish()
        name = "gDPSetScissorFrac"
    elif opcode == 0xEE:
        lo = Word.parse(w1)
        args = [lo.take(16, 16), lo.take(0, 16)]
        lo.finish()
        name = "gDPSetPrimDepth"
    elif opcode == 0xFC:
        lo = Word.parse(w1)
        # Context-sensitive mux values: 0/1 have different encodings per input.
        color = {0: "COMBINED", 1: "TEXEL0", 2: "TEXEL1", 3: "PRIMITIVE", 4: "SHADE", 5: "ENVIRONMENT"}

        def mux(word: Word, shift: int, width: int, extra: dict[int, str]) -> str:
            value = word.fixed(shift, width)
            choices = {**color, **extra}
            if value not in choices:
                raise Ambiguous("unknown combine mux")
            return choices[value]

        args = [
            mux(hi, 20, 4, {6: "1", 7: "NOISE", 15: "0"}),
            mux(lo, 28, 4, {6: "CENTER", 7: "K4", 15: "0"}),
            mux(
                hi,
                15,
                5,
                {
                    6: "SCALE",
                    7: "COMBINED_ALPHA",
                    8: "TEXEL0_ALPHA",
                    9: "TEXEL1_ALPHA",
                    10: "PRIMITIVE_ALPHA",
                    11: "SHADE_ALPHA",
                    12: "ENV_ALPHA",
                    13: "LOD_FRACTION",
                    14: "PRIM_LOD_FRAC",
                    15: "K5",
                    31: "0",
                },
            ),
            mux(lo, 15, 3, {6: "1", 7: "0"}),
            mux(hi, 12, 3, {6: "1", 7: "0"}),
            mux(lo, 12, 3, {6: "1", 7: "0"}),
            mux(hi, 9, 3, {0: "LOD_FRACTION", 6: "PRIM_LOD_FRAC", 7: "0"}),
            mux(lo, 9, 3, {6: "1", 7: "0"}),
            mux(hi, 5, 4, {6: "1", 7: "NOISE", 15: "0"}),
            mux(lo, 24, 4, {6: "CENTER", 7: "K4", 15: "0"}),
            mux(
                hi,
                0,
                5,
                {
                    6: "SCALE",
                    7: "COMBINED_ALPHA",
                    8: "TEXEL0_ALPHA",
                    9: "TEXEL1_ALPHA",
                    10: "PRIMITIVE_ALPHA",
                    11: "SHADE_ALPHA",
                    12: "ENV_ALPHA",
                    13: "LOD_FRACTION",
                    14: "PRIM_LOD_FRAC",
                    15: "K5",
                    31: "0",
                },
            ),
            mux(lo, 6, 3, {6: "1", 7: "0"}),
            mux(lo, 21, 3, {6: "1", 7: "0"}),
            mux(lo, 3, 3, {6: "1", 7: "0"}),
            mux(lo, 18, 3, {0: "LOD_FRACTION", 6: "PRIM_LOD_FRAC", 7: "0"}),
            mux(lo, 0, 3, {6: "1", 7: "0"}),
        ]
        lo.finish()
        name = "gDPSetCombineLERP"
    else:
        if variant is None:
            raise Ambiguous("RSP microcode variant is not established")
        if opcode in ((0xE2, 0xE3) if variant == "f3dex2" else (0xB9, 0xBA)):
            shift, length = hi.fixed(8, 8), hi.fixed(0, 8)
            if variant == "f3dex2":
                length += 1
                shift = 32 - shift - length
            if shift < 0 or length < 1 or shift + length > 32:
                raise Ambiguous("invalid other-mode shift/length")
            high = opcode in (0xE3, 0xBA)
            if high and (shift, length) in OTHER_H:
                suffix, options = OTHER_H[shift, length]
                table = dict(enumerate(options)) if isinstance(options, list) else options
                name, args = "gDPSet" + suffix, [number(w1, {key << shift: val for key, val in table.items()})]
            elif not high and (shift, length) == (3, 29):
                terms = split(unwrap(w1), "|")
                args = terms if len(terms) == 2 else [w1, "0"]
                name = "gDPSetRenderMode"
            elif not high and (shift, length) in ((0, 2), (2, 1)):
                name = "gDPSetAlphaCompare" if shift == 0 else "gDPSetDepthSource"
                args = [
                    number(
                        w1,
                        {0: "G_AC_NONE", 1: "G_AC_THRESHOLD", 3: "G_AC_DITHER"}
                        if shift == 0
                        else {0: "G_ZS_PIXEL", 4: "G_ZS_PRIM"},
                    )
                ]
            else:
                name = "gSPSetOtherMode"
                args = ["G_SETOTHERMODE_H" if high else "G_SETOTHERMODE_L", str(shift), str(length), w1]
        elif opcode == (0xDE if variant == "f3dex2" else 0x06):
            branch = hi.fixed(16, 8)
            if branch not in (0, 1):
                raise Ambiguous("unknown display-list branch flag")
            name, args = ("gSPBranchList" if branch else "gSPDisplayList"), [w1]
        elif opcode == (0xDF if variant == "f3dex2" else 0xB8):
            if integer(w1) != 0:
                raise Ambiguous("end display-list payload must be zero")
            name, args = "gSPEndDisplayList", []
        elif opcode == (0xDA if variant == "f3dex2" else 0x01):
            if variant == "f3dex2":
                if hi.fixed(19, 5) != 7:
                    raise Ambiguous("matrix DMA size is not 64 bytes")
                param = hi.fixed(0, 8) ^ 1
                choices = {1: "G_MTX_PUSH", 2: "G_MTX_LOAD", 4: "G_MTX_PROJECTION"}
            else:
                if hi.fixed(0, 16) != 64:
                    raise Ambiguous("matrix DMA size is not 64 bytes")
                param = hi.fixed(16, 8)
                choices = {1: "G_MTX_PROJECTION", 2: "G_MTX_LOAD", 4: "G_MTX_PUSH"}
            name, args = "gSPMatrix", [w1, flags(str(param), choices)]
        elif opcode == (0xDB if variant == "f3dex2" else 0xBC):
            index = hi.fixed(16 if variant == "f3dex2" else 0, 8)
            offset = hi.fixed(0 if variant == "f3dex2" else 8, 16)
            if index == 6 and offset % 4 == 0:
                name, args = "gSPSegment", [str(offset // 4), w1]
            else:
                name, args = (
                    "gSPMoveWord",
                    [
                        number(
                            str(index),
                            {
                                0: "G_MW_MATRIX",
                                2: "G_MW_NUMLIGHT",
                                4: "G_MW_CLIP",
                                8: "G_MW_FOG",
                                10: "G_MW_LIGHTCOL",
                                14: "G_MW_PERSPNORM",
                            },
                        ),
                        str(offset),
                        w1,
                    ],
                )
        elif variant == "f3dex2" and opcode == 0xDC:
            size, offset, index = (hi.fixed(19, 5) + 1) * 8, hi.fixed(8, 8) * 8, hi.fixed(0, 8)
            name, args = (
                "gSPMoveMem",
                [
                    number(str(index), {8: "G_MV_VIEWPORT", 10: "G_MV_LIGHT", 14: "G_MV_MATRIX"}),
                    str(offset),
                    str(size),
                    w1,
                ],
            )
        elif variant == "f3dex2" and opcode == 0xD9:
            clear = hi.fixed(0, 24) ^ 0xFFFFFF
            name, args = (
                "gSPGeometryMode",
                [
                    flags(
                        str(clear),
                        {
                            1: "G_ZBUFFER",
                            4: "G_SHADE",
                            0x200: "G_CULL_FRONT",
                            0x400: "G_CULL_BACK",
                            0x10000: "G_FOG",
                            0x20000: "G_LIGHTING",
                            0x40000: "G_TEXTURE_GEN",
                            0x80000: "G_TEXTURE_GEN_LINEAR",
                            0x200000: "G_SHADING_SMOOTH",
                        },
                    ),
                    flags(
                        w1,
                        {
                            1: "G_ZBUFFER",
                            4: "G_SHADE",
                            0x200: "G_CULL_FRONT",
                            0x400: "G_CULL_BACK",
                            0x10000: "G_FOG",
                            0x20000: "G_LIGHTING",
                            0x40000: "G_TEXTURE_GEN",
                            0x80000: "G_TEXTURE_GEN_LINEAR",
                            0x200000: "G_SHADING_SMOOTH",
                        },
                    ),
                ],
            )
        elif variant == "f3dex2" and opcode == 0xD7:
            lo = Word.parse(w1)
            args = [
                lo.take(16, 16),
                lo.take(0, 16),
                hi.take(11, 3),
                hi.take(8, 3),
                number(hi.take(1, 7), {0: "G_OFF", 1: "G_ON"}),
            ]
            lo.finish()
            name = "gSPTexture"
        elif variant == "f3dex2" and opcode == 0x01:
            n, end = hi.fixed(12, 8), hi.fixed(1, 7)
            if end < n:
                raise Ambiguous("vertex destination precedes count")
            name, args = "gSPVertex", [w1, str(n), str(end - n)]
        elif variant == "f3dex2" and opcode in (0x05, 0x06):
            args = []
            words = [hi] if opcode == 5 else [hi, Word.parse(w1)]
            for word in words:
                vertices = [word.fixed(shift, 8) for shift in (16, 8, 0)]
                if any(value % 2 for value in vertices):
                    raise Ambiguous("odd triangle vertex index")
                args += [*(str(value // 2) for value in vertices), "0"]
                word.finish()
            if opcode == 5 and integer(w1) != 0:
                raise Ambiguous("triangle payload must be zero")
            name = "gSP1Triangle" if opcode == 5 else "gSP2Triangles"
        elif variant == "f3dex2" and opcode == 0x02:
            where, vertex = hi.fixed(16, 8), hi.fixed(0, 16)
            if vertex % 2:
                raise Ambiguous("odd modify-vertex index")
            name, args = (
                "gSPModifyVertex",
                [
                    str(vertex // 2),
                    number(
                        str(where),
                        {
                            0x10: "G_MWO_POINT_RGBA",
                            0x14: "G_MWO_POINT_ST",
                            0x18: "G_MWO_POINT_XYSCREEN",
                            0x1C: "G_MWO_POINT_ZSCREEN",
                        },
                    ),
                    w1,
                ],
            )
        elif opcode in ((0xE1, 0xF1) if variant == "f3dex2" else (0xB4, 0xB3)):
            name, args = ("gDPHalf1" if opcode in (0xE1, 0xB4) else "gDPHalf2"), [w1]
        else:
            raise Ambiguous(f"unsupported opcode 0x{opcode:02X}")
    hi.finish()
    return name, args


@dataclass
class Raw:
    line: int
    command: str
    reason: str


@dataclass
class Lowered:
    source: str
    macros: Counter[str] = field(default_factory=Counter)
    raw: list[Raw] = field(default_factory=list)


# Mask lexical material without changing offsets, so comments/directives cannot
# be mistaken for executable writes. Comments between the stores are retained.
LEXICAL = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|^[ \t]*\#(?:[^\n]*\\\n)*[^\n]*', re.S | re.M
)
ACCESS = r"(?P<ptr>[A-Za-z_]\w*(?:\s*\[[^\]\n]+\])*)\s*(?P<access>->|\.)(?:\s*words\s*\.)?\s*w0"
PAIR = re.compile(
    ACCESS + r"\s*=\s*(?P<w0>[^;]+);\s*(?P=ptr)\s*(?P=access)(?:\s*words\s*\.)?\s*w1\s*=\s*(?P<w1>[^;]+);", re.S
)


def lower(source: str, variant: str | None = None) -> Lowered:
    result = Lowered(source)
    definitions = macros(source)
    shiftl = definitions.get("_SHIFTL")
    unsafe_shiftl = bool(re.search(r"^\s*#\s*define\s+_SHIFTL\s*\(", source, re.M)) and (
        shiftl is None or not standard_shiftl(shiftl)
    )
    masked = LEXICAL.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), source)
    edits = []
    for match in PAIR.finditer(masked):
        w0, w1 = (expand(source[match.start(key) : match.end(key)].strip(), definitions) for key in ("w0", "w1"))
        line = source.count("\n", 0, match.start()) + 1
        try:
            if unsafe_shiftl:
                raise Ambiguous("local _SHIFTL definition is not proven standard")
            if re.search(r"\b(?:volatile\s+Gfx|Gfx\s+volatile)\s*\*\s*" + re.escape(match["ptr"]) + r"\b", masked):
                raise Ambiguous("volatile packet requires volatile stores; standard builder discards the qualifier")
            if not pure(match["ptr"]) or "_gbi" in match["ptr"]:
                raise Ambiguous("packet expression has effects or captures the builder temporary")
            if re.search(r"^\s*#", source[match.start() : match.end()], re.M):
                raise Ambiguous("preprocessor boundary between stores")
            before = masked[masked.rfind("\n", 0, match.start()) + 1 : match.start()]
            if re.search(r"\b(?:if|while|for)\s*\([^{};]*\)\s*$", before):
                raise Ambiguous("only the first store is controlled by a branch")
            if "words" in w0 + w1:
                raise Ambiguous("operand reads command storage")
            name, args = decode(w0, w1, variant)
        except Ambiguous as error:
            try:
                word = Word.parse(w0)
                opcode = word.fixed(24, 8)
                command = OP_NAMES.get(opcode, f"opcode 0x{opcode:02X}")
            except Ambiguous:
                command = "computed opcode"
            result.raw.append(Raw(line, command, str(error)))
            continue
        pointer = match["ptr"] if match["access"] == "->" else "&" + match["ptr"]
        start, end = match.span()
        # Collapse a dedicated three-statement builder block. Other pointer
        # assignments are retained because the temporary may escape or be reused.
        prefix = re.search(r"\{\s*Gfx\s*\*\s*" + re.escape(match["ptr"]) + r"\s*=\s*([^;{}]+);\s*$", masked[:start])
        suffix = re.match(r"\s*\}", masked[end:])
        scoped = bool(prefix and suffix)
        if prefix and suffix:
            pointer = source[prefix.start(1) : prefix.end(1)].strip()
            start, end = prefix.start(), end + suffix.end()
        if not scoped and len(re.findall(r"\b" + re.escape(match["ptr"]) + r"\b", masked)) == 4:
            assigned = re.search(r"\b" + re.escape(match["ptr"]) + r"\s*=\s*([^;{}]+);\s*$", masked[:start])
            if assigned and re.search(r"\bGfx\s*\*\s*" + re.escape(match["ptr"]) + r"\s*;", masked):
                pointer = source[assigned.start(1) : assigned.end(1)].strip()
                start = assigned.start()
        comments = re.findall(r"/\*.*?\*/|//[^\n]*", source[start:end], re.S)
        text = "\n".join([*comments, f"{name}({', '.join([pointer, *args])});"])
        if scoped and re.match(r"\s*else\b", masked[end:]):
            text = text.removesuffix(";")
        edits.append((start, end, text))
        result.macros[name] += 1
    for start, end, _, _ in initializer_pairs(masked):
        # Recover actual operands from the lexical offsets, including symbols.
        element = source[start:end]
        values = split(re.sub(r"^\{\s*\{|\}\s*\}$", "", element).strip(), ",")
        if len(values) != 2:
            continue
        try:
            if unsafe_shiftl:
                raise Ambiguous("local _SHIFTL definition is not proven standard")
            hi_expr, lo_expr = (expand(value, definitions) for value in values)
            name, args = decode(hi_expr, lo_expr, variant)
        except Ambiguous as error:
            result.raw.append(Raw(source.count("\n", 0, start) + 1, "Gfx initializer", str(error)))
            continue
        name = "gs" + name[1:]
        edits.append((start, end, f"{name}({', '.join(args)})"))
        result.macros[name] += 1
    for macro in definitions.values():
        builder = word_builder(macro, definitions)
        if builder is None:
            continue
        sites = invocations(masked, macro.name)
        converted = 0
        for start, end, _ in sites:
            actual = invocations(source[start:end], macro.name)[0][2]
            bindings = dict(zip(macro.parameters, actual, strict=False))

            def substitute(text: str, bindings: dict[str, str] = bindings) -> str:
                return re.sub(r"\b\w+\b", lambda m: "(" + bindings[m[0]] + ")" if m[0] in bindings else m[0], text)

            pointer, hi_expr, lo_expr = (substitute(value) for value in builder)
            pointer = unwrap(pointer)
            if cast := re.fullmatch(r"\(\s*Gfx\s*\*\s*\)\s*(.+)", pointer, re.S):
                pointer = unwrap(cast[1])
            try:
                if unsafe_shiftl:
                    raise Ambiguous("local _SHIFTL definition is not proven standard")
                name, args = decode(expand(hi_expr, definitions), expand(lo_expr, definitions), variant)
            except Ambiguous as error:
                result.raw.append(Raw(source.count("\n", 0, start) + 1, macro.name, str(error)))
                continue
            # A legacy builder is a statement; consume its optional semicolon.
            tail = re.match(r"[ \t]*;", source[end:])
            if tail:
                end += tail.end()
            edits.append((start, end, f"{name}({', '.join([pointer, *args])});"))
            result.macros[name] += 1
            converted += 1
        if converted == len(sites):
            edits.append((macro.start, macro.end, ""))
    # Report unmatched writes as well as paired commands.
    starts = {match.start() for match in PAIR.finditer(masked)}
    for match in re.finditer(ACCESS + r"\s*=", masked):
        if match.start() not in starts:
            result.raw.append(
                Raw(
                    source.count("\n", 0, match.start()) + 1,
                    "unpaired word write",
                    "stores are not a consecutive w0/w1 pair",
                )
            )
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    result.source = source
    return result


def microcode(project: Project) -> str | None:
    from unbake.project.makefile import recipe

    build = recipe(project)
    definitions = " ".join(
        [*build.cppflags, *(flag for compiler in project.compilers.values() for flag in compiler.cflags)]
    )
    variants = [
        variant
        for macro, variant in (("F3DEX_GBI_2", "f3dex2"), ("F3DEX_GBI", "f3dex"), ("F3D_GBI", "f3d"))
        if re.search(r"(?:^|-D|\s)" + macro + r"(?:=|\s|$)", definitions)
    ]
    if len(variants) > 1:
        raise Held("gbi", "conflicting microcode definitions: " + ", ".join(variants))
    return variants[0] if variants else None


def install(project: Project) -> str:
    if not project.include:
        raise Held("gbi", "paths.include: required include directory")
    destination = project.include[0] / "gbi.h"
    content = HEADER.read_text()
    if destination.is_symlink() or (destination.exists() and destination.read_text() != content):
        raise Held("gbi", f"{destination}: existing header differs from the open reconstruction")
    for root in project.include:
        sdk = root / "n64sdk.h"
        if sdk.is_file() and not re.search(r"^\s*#\s*(?:ifndef|pragma\s+once)\b", sdk.read_text(), re.M):
            if sdk.is_symlink():
                raise Held("gbi", f"{sdk}: SDK type header must be regular")
            sdk.write_text("#ifndef UNBAKE_N64SDK_H\n#define UNBAKE_N64SDK_H\n" + sdk.read_text() + "\n#endif\n")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_text(content)
    sdk_include = '#include "n64sdk.h"\n' if any((root / "n64sdk.h").is_file() for root in project.include) else ""
    return sdk_include + '#include "gbi.h"\n'


def canonical_types(project: Project, source: str) -> str:
    """Reuse an existing SDK Gfx type instead of retaining per-unit imitations."""
    sdk = next((root / "n64sdk.h" for root in project.include if (root / "n64sdk.h").is_file()), None)
    if sdk is None:
        return source
    spans = gfx_typedefs(source)
    if not spans:
        return source
    shared_gfx = sdk.parent / "shared/gfx.h"
    sdk_text = sdk.read_text() + ("\n" + shared_gfx.read_text() if shared_gfx.is_file() else "")
    sdk_types = typedefs(sdk_text)
    source_types = typedefs(source)
    removals = []
    renames = {}
    for name, (start, end, declaration) in source_types.items():
        if name in sdk_types and (name == "Gfx" or tokens(declaration) == tokens(sdk_types[name][2])):
            removals.append((start, end))
        elif name in sdk_types:
            local = "Unit" + name
            if re.search(r"\b" + local + r"\b", source):
                raise Held("gbi", f"local SDK type collision: {local}")
            renames[name] = local
    # The legacy two-field struct has the same byte layout as Gfx.words.
    direct = any("words" not in source[start:end] for start, end in spans)
    if direct:
        names = set(re.findall(r"\bGfx\s*\*\s*(\w+)", source))
        # Apply access normalization after removing declarations to keep offsets.
    scalar = next(
        (
            path
            for root in project.include
            for path in sorted(root.glob("*.h"))
            if all(
                re.search(r"\btypedef\b[^;]+\b" + name + r"\s*;", path.read_text())
                for name in ("u8", "u16", "u32", "s32", "s64", "f32")
            )
        ),
        None,
    )
    if scalar is None:
        raise Held("gbi", "n64sdk.h: cannot establish its scalar type include")
    includes = f'#include "{scalar.name}"\n#include "n64sdk.h"'
    for start, end in sorted(removals, reverse=True):
        source = source[:start] + includes + source[end:]
        includes = ""

    if direct:
        for name in names:
            source = re.sub(
                r"\b" + re.escape(name) + r"(\s*->\s*)(w[01])\b",
                lambda match: match[0].rsplit(match[2], 1)[0] + "words." + match[2],
                source,
            )
    for name, replacement in renames.items():
        source = re.sub(r"\b" + re.escape(name) + r"\b", replacement, source)
    return source


def rewrite(project: Project, files: list[Path], *, all_files: bool = False) -> dict[str, object]:
    if bool(files) == all_files:
        raise Held("gbi", "provide FILE... or --all")
    paths = sorted(project.src.rglob("*.c")) if all_files else files
    variant = microcode(project)
    counts: Counter[str] = Counter()
    raw = []
    changed = []
    prepared = []
    for path in paths:
        path = path if path.is_absolute() else project.root / path
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(project.root):
            raise Held("gbi", f"{path}: required project-local regular C file")
        source = path.read_text()
        result = lower(source, variant)
        raw += [
            {
                "file": str(path.relative_to(project.root)),
                "line": item.line,
                "command": item.command,
                "reason": item.reason,
            }
            for item in result.raw
        ]
        if result.macros:
            result.source = canonical_types(project, result.source)
            prepared.append((path, source, result))
            counts.update(result.macros)
    if prepared:
        include = install(project)
        for path, source, result in prepared:
            output = result.source
            if '#include "gbi.h"' not in output:
                # Types must precede GBI use; macros expand at their call sites.
                output = include + output
            if output != source:
                path.write_text(output)
                changed.append(str(path.relative_to(project.root)))
    return {
        "variant": variant,
        "files_rewritten": len(changed),
        "files": changed,
        "macros": dict(sorted(counts.items())),
        "raw": raw,
    }
