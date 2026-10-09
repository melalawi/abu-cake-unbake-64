"""Mocked resource-driven GBI recovery, including revision-3 encodings."""

from copy import deepcopy

import pytest

from unbake import config, gbi


@pytest.fixture
def commands(monkeypatch):
    document = deepcopy(config.load_resource("gbi.toml"))

    def load(name):
        assert name == "gbi.toml"
        return document

    monkeypatch.setattr(gbi.config, "load_resource", load)
    return document


@pytest.fixture
def basic(commands):
    commands["command"] = [
        row for row in commands["command"] if row["macro"] in {"gDPPipeSync", "gSPDisplayList", "gDPSetPrimColor"}
    ]
    return commands


def _pair(w0, w1, pointer="p", access="->"):
    return f"{pointer}{access}words.w0 = {w0}; {pointer}{access}words.w1 = {w1};"


def _expect(text, macro):
    rewritten, count = gbi.rewrite(text)
    assert count == 1
    assert rewritten.strip() == macro
    assert gbi.rewrite(rewritten) == (rewritten, 0)


def test_pipe_sync(basic):
    _expect(_pair("0xE7000000", "0"), "gDPPipeSync(p);")


@pytest.mark.parametrize("operand", ["sym", "base + offset", "(u32) &display_list[2]"])
def test_display_list_expression_operand(basic, operand):
    _expect(_pair("0xDE000000", operand), f"gSPDisplayList(p, {operand});")


def test_fields_decoded_from_literal(basic):
    _expect(_pair("0xFA000A02", "0x01020AFF"), "gDPSetPrimColor(p, 0xa, 2, 1, 2, 0xa, 0xff);")


def test_unknown_opcode_untouched(basic):
    text = _pair("0x99000000", "sym")
    assert gbi.rewrite(text) == (text, 0)


def test_shifted_term_field(basic):
    text = _pair("0xFA000000 | ((x & 0xFF) << 8)", "0x01020304")
    _expect(text, "gDPSetPrimColor(p, x, 0, 1, 2, 3, 4);")


@pytest.mark.parametrize("pointer,access", [("p", "->"), ("p", "."), ("P[k]", "."), ("ctx->p", "->")])
def test_pointer_forms(basic, pointer, access):
    _expect(_pair("0xE7000000", "0", pointer, access), f"gDPPipeSync({pointer});")


@pytest.mark.parametrize(
    "gap,rewrites", [("", True), ("a = 1;", True), ("a = 1; b = 2;", True), ("a = 1; b = 2; c = 3;", False)]
)
def test_three_statement_window(basic, gap, rewrites):
    text = f"p->words.w0 = 0xE7000000; {gap} p->words.w1 = 0;"
    out, count = gbi.rewrite(text)
    assert count == int(rewrites)
    if rewrites:
        assert out.strip() == f"gDPPipeSync(p); {gap}".strip()
    else:
        assert out == text


@pytest.mark.parametrize(
    "text",
    [
        "p->words.w0 = 0xE7000000; q->words.w1 = 0;",
        "p->words.w1 = 0; p->words.w0 = 0xE7000000;",
        "p->words.w0 = 0xE7000000; p->words.w0 = unknown; p->words.w1 = 0;",
        "p->words.w0 = 0xE7000000; } { p->words.w1 = 0;",
        'const char *s = "p->words.w0 = 0xE7000000; p->words.w1 = 0;";',
        "/* p->words.w0 = 0xE7000000; p->words.w1 = 0; */",
        "// p->words.w0 = 0xE7000000; p->words.w1 = 0;\n",
    ],
)
def test_unpaired_or_opaque_text_preserved(basic, text):
    assert gbi.rewrite(text) == (text, 0)


def test_multiple_pairs_and_surrounding_text(basic):
    text = "void f() {\n  " + _pair("0xE7000000", "0") + "\n  " + _pair("0xDE000000", "sym") + "\n}\n"
    out, count = gbi.rewrite(text)
    assert count == 2
    assert out == "void f() {\n  gDPPipeSync(p); \n  gSPDisplayList(p, sym); \n}\n"


@pytest.mark.parametrize("literal", ["0XE7000000UL", "3875536896u", "034700000000", "(0xE7000000)"])
def test_integer_spellings(basic, literal):
    _expect(_pair(literal, "0"), "gDPPipeSync(p);")


@pytest.mark.parametrize(
    "w0,w1",
    [
        ("0xE7000001", "0"),
        ("0xE7000000", "1"),
        ("0xDE020000", "sym"),
        ("0xDE000001", "sym"),
        ("0xFA010000", "0"),
        ("0xFA000000", "sym"),
        ("0xFA000000 | ((x & 0x7F) << 8)", "0"),
        ("0xFA000000 | ((x & 0xFF) << 7)", "0"),
        ("0xFA000100 | ((x & 0xFF) << 8)", "0"),
        ("0xFA000000 | ((x & 0xFF) << 8) | ((y & 0xFF) << 8)", "0"),
        ("0xFA000000 | ((x & 0xA5) << 8)", "0"),
        ("0xFA000000 | ((x & 0xFF) << 32)", "0"),
        ("0x1E7000000", "0"),
    ],
)
def test_undecodable_bits_and_fields_preserved(basic, w0, w1):
    text = _pair(w0, w1)
    assert gbi.rewrite(text) == (text, 0)


@pytest.mark.parametrize(
    "w0,w1",
    [
        ("0xDE000000", "next()"),
        ("0xDE000000", "sym++"),
        ("0xDE000000", "--sym"),
        ("0xDE000000", "(sym = other)"),
        ("0xFA000000 | ((next() & 0xFF) << 8)", "0"),
        ("0xFA000000 | ((x++ & 0xFF) << 8)", "0"),
    ],
)
def test_side_effect_expressions_preserved(basic, w0, w1):
    text = _pair(w0, w1)
    assert gbi.rewrite(text) == (text, 0)


@pytest.mark.parametrize("pointer", ["p[k++]", "p[next()]", "p[k = 1]"])
def test_side_effect_pointer_preserved(basic, pointer):
    text = _pair("0xE7000000", "0", pointer, ".")
    assert gbi.rewrite(text) == (text, 0)


@pytest.mark.parametrize(
    "w0,w1,macro",
    [
        ("0xDB06000C", "base", "gSPSegment(p, 3, base);"),
        ("0xBC000C06", "base", "gSPSegment(p, 3, base);"),
        ("0xDB080004", "value", "gSPMoveWord(p, G_MW_FOG, 4, value);"),
        ("0xBC000408", "value", "gSPMoveWord(p, G_MW_FOG, 4, value);"),
        ("0xDC080208", "address", "gSPMoveMem(p, G_MV_VIEWPORT, 0x10, 0x10, address);"),
        ("0xFD10013F", "image", "gDPSetTextureImage(p, G_IM_FMT_RGBA, G_IM_SIZ_16b, 0x140, image);"),
        ("0xFF100000", "image", "gDPSetColorImage(p, G_IM_FMT_RGBA, G_IM_SIZ_16b, 1, image);"),
        ("0xDA380006", "matrix", "gSPMatrix(p, matrix, G_MTX_PUSH | G_MTX_LOAD | G_MTX_PROJECTION);"),
        ("0x01070040", "matrix", "gSPMatrix(p, matrix, G_MTX_PROJECTION | G_MTX_LOAD | G_MTX_PUSH);"),
        ("0xD9FFFFFE", "0x20000", "gSPGeometryMode(p, G_ZBUFFER, G_LIGHTING);"),
        ("0xD7000A02", "0x00100020", "gSPTexture(p, 0x10, 0x20, 1, 2, G_ON);"),
        ("0xD7030A02", "0x00100020", "gSPTextureL(p, 0x10, 0x20, 1, 3, 2, G_ON);"),
        ("0xE3001A01", "0x20", "gDPSetAlphaDither(p, G_AD_NOISE);"),
        ("0xE3001201", "0x2000", "gDPSetTextureFilter(p, G_TF_BILERP);"),
        ("0xBA000C02", "0x2000", "gDPSetTextureFilter(p, G_TF_BILERP);"),
        ("0xE2001E01", "1", "gDPSetAlphaCompare(p, G_AC_THRESHOLD);"),
        ("0xE2000503", "0x1234", "gSPSetOtherMode(p, G_SETOTHERMODE_L, 0x17, 4, 0x1234);"),
        ("0xE3000503", "data", "gSPSetOtherMode(p, G_SETOTHERMODE_H, 0x17, 4, data);"),
        ("0x0100300A", "vertices", "gSPVertex(p, vertices, 3, 2);"),
        ("0x05020406", "0", "gSP1Triangle(p, 1, 2, 3, 0);"),
        ("0x06020406", "0x00080A0C", "gSP2Triangles(p, 1, 2, 3, 0, 4, 5, 6, 0);"),
    ],
)
def test_schema2_encodings(commands, w0, w1, macro):
    _expect(_pair(w0, w1), macro)


@pytest.mark.parametrize(
    "w0,w1",
    [
        ("0xDA300006", "matrix"),
        ("0xDA380106", "matrix"),
        ("0x05030406", "0"),
        ("0x05020406", "1"),
        ("0x01003004", "vertices"),
        ("0xFC800000", "0"),
        ("0xFC080000", "0"),
        ("0xE2001F02", "data"),
        ("0xE2002000", "data"),
    ],
)
def test_schema2_invalid_encodings(commands, w0, w1):
    text = _pair(w0, w1)
    assert gbi.rewrite(text) == (text, 0)


def test_combine_lerp_choices_and_order(commands):
    w0 = 0xFC000000 | (1 << 20) | (8 << 15) | (6 << 12) | (6 << 9) | (15 << 5) | 31
    w1 = (7 << 28) | (6 << 24) | (5 << 21) | (6 << 18) | (7 << 15) | (2 << 12) | (3 << 9) | (4 << 6) | (1 << 3)
    _expect(
        _pair(hex(w0), hex(w1)),
        "gDPSetCombineLERP(p, TEXEL0, K4, TEXEL0_ALPHA, 0, 1, TEXEL1, PRIM_LOD_FRAC, PRIMITIVE, "
        "0, CENTER, 0, SHADE, ENVIRONMENT, TEXEL0, PRIM_LOD_FRAC, COMBINED);",
    )


@pytest.mark.parametrize(
    "define,w1,macro",
    [
        ("F3D_GBI", "0x000A141E", "gSP1Triangle(p, 1, 2, 3, 0);"),
        ("F3DEX_GBI", "0x000A141E", "gSP1Triangle(p, 5, 0xa, 0xf, 0);"),
    ],
)
def test_classic_triangle_define_evidence(commands, define, w1, macro):
    text = f"#define {define}\nvoid f() {{ " + _pair("0xBF000000", w1) + " }"
    out, count = gbi.rewrite(text)
    assert count == 1
    assert out == f"#define {define}\nvoid f() {{ {macro}  }}"


def test_ambiguous_classic_triangle_preserved(commands):
    text = _pair("0xBF000000", "0x000A141E")
    assert gbi.rewrite(text) == (text, 0)


@pytest.mark.parametrize("w0,w1,rewrites", [("0x0100300A", "vertices", True), ("0xBF000000", "0x000A141E", False)])
def test_f3dex2_define_filters_rows(commands, w0, w1, rewrites):
    text = "#define F3DEX_GBI_2\nvoid f() { " + _pair(w0, w1) + " }"
    out, count = gbi.rewrite(text)
    assert count == int(rewrites)
    if rewrites:
        assert "gSPVertex(p, vertices, 3, 2);" in out
    else:
        assert out == text


def test_unique_opcode_supplies_variant_evidence(commands):
    text = _pair("0x05020406", "0") + " " + _pair("0x01000040", "vertices")
    out, count = gbi.rewrite(text)
    assert count == 2
    assert out.strip() == "gSP1Triangle(p, 1, 2, 3, 0);  gSPVertex(p, vertices, 0, 0x20);"


def test_fixed_match_specificity_beats_resource_order(commands):
    commands["command"].reverse()
    _expect(_pair("0xDB06000C", "base"), "gSPSegment(p, 3, base);")


def test_exact_transform_divisibility(commands):
    commands["command"] = [row for row in commands["command"] if row["macro"] == "gSPSegment"]
    text = _pair("0xDB060003", "base")
    assert gbi.rewrite(text) == (text, 0)


def test_symbolic_divisor_recovery(commands):
    text = _pair("0xDB060000 | (((segment * 4) & 0xFFFF) << 0)", "base")
    _expect(text, "gSPSegment(p, segment, base);")


def test_transform_order(commands):
    commands["command"] = [
        {
            "macro": "fixtureMacro",
            "opcode": 0x90,
            "words": 2,
            "fields": [
                {"name": "value", "word": 0, "shift": 0, "bits": 8, "xor": 3, "divisor": 2, "bias": 1, "scale": 8}
            ],
        }
    ]
    _expect(_pair("0x90000005", "0"), "fixtureMacro(p, 0x20);")
    text = _pair("0x90000004", "0")
    assert gbi.rewrite(text) == (text, 0)
