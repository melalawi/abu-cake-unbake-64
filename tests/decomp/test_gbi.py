"""GBI recovery, conservative failures, and independent command-word proofs."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.decomp.support import fixture
from unbake.decomp import gbi
from unbake.decomp.gbi_expr import Ambiguous
from unbake.project.config import Held, Policy


class GbiTests(unittest.TestCase):
    def test_command_families(self) -> None:
        cases = [
            ("0xE7000000", "0", "gDPPipeSync", []),
            ("0xE6000000", "0", "gDPLoadSync", []),
            ("0xE8000000", "0", "gDPTileSync", []),
            ("0xE9000000", "0", "gDPFullSync", []),
            ("0xE3000A01", "0x300000", "gDPSetCycleType", ["G_CYC_FILL"]),
            ("0xE3001201", "0x2000", "gDPSetTextureFilter", ["G_TF_BILERP"]),
            ("0xE200001C", "first | second", "gDPSetRenderMode", ["first", "second"]),
            ("0xFF10013F", "(u32)frame", "gDPSetColorImage", ["G_IM_FMT_RGBA", "G_IM_SIZ_16b", "320", "(u32)frame"]),
            ("0xFD900000", "texture", "gDPSetTextureImage", ["G_IM_FMT_I", "G_IM_SIZ_16b", "1", "texture"]),
            ("0xFA00FFFF", "0xC8000096", "gDPSetPrimColor", ["255", "255", "200", "0", "0", "150"]),
            ("0xFB000000", "0x12345678", "gDPSetEnvColor", ["18", "52", "86", "120"]),
            ("0xF7000000", "color", "gDPSetFillColor", ["color"]),
            ("0xFE000000", "image", "gDPSetDepthImage", ["image"]),
            (
                "0xF6000000 | ((right & 0x3FF) << 14) | ((bottom & 0x3FF) << 2)",
                "((left & 0x3FF) << 14) | ((top & 0x3FF) << 2)",
                "gDPFillRectangle",
                ["left", "top", "right", "bottom"],
            ),
            (
                "0xF5900000",
                "0x07080200",
                "gDPSetTile",
                [
                    "G_IM_FMT_I",
                    "G_IM_SIZ_16b",
                    "0",
                    "0",
                    "G_TX_LOADTILE",
                    "0",
                    "G_TX_CLAMP",
                    "0",
                    "0",
                    "G_TX_CLAMP",
                    "0",
                    "0",
                ],
            ),
            ("0xF3000000", "0x0707F400", "gDPLoadBlock", ["G_TX_LOADTILE", "0", "0", "127", "1024"]),
            ("0xF2000000", "0x0003C03C", "gDPSetTileSize", ["G_TX_RENDERTILE", "0", "0", "60", "60"]),
            ("0xF0000000", "0x073FC000", "gDPLoadTLUTCmd", ["G_TX_LOADTILE", "255"]),
            ("0xED000000", "0x005003C0", "gDPSetScissorFrac", ["G_SC_NON_INTERLACE", "0", "0", "1280", "960"]),
            ("0xEE000000", "0x12345678", "gDPSetPrimDepth", ["4660", "22136"]),
            ("0xDB060004", "base", "gSPSegment", ["1", "base"]),
            ("0xDA380003", "matrix", "gSPMatrix", ["matrix", "G_MTX_LOAD"]),
            ("0xDC08060A", "light", "gSPMoveMem", ["G_MV_LIGHT", "48", "16", "light"]),
            ("0xD9FFFFFF", "0x200004", "gSPGeometryMode", ["0", "G_SHADE | G_SHADING_SMOOTH"]),
            ("0xD7000002", "0x80008000", "gSPTexture", ["32768", "32768", "0", "0", "G_ON"]),
            ("0xDE000000", "list", "gSPDisplayList", ["list"]),
            ("0xDE010000", "list", "gSPBranchList", ["list"]),
            ("0xDF000000", "0", "gSPEndDisplayList", []),
            ("0x01004008", "vertices", "gSPVertex", ["vertices", "4", "0"]),
            ("0x05000204", "0", "gSP1Triangle", ["0", "1", "2", "0"]),
            ("0x06000204", "0x00040600", "gSP2Triangles", ["0", "1", "2", "0", "2", "3", "0", "0"]),
            ("0x021C0004", "depth", "gSPModifyVertex", ["2", "G_MWO_POINT_ZSCREEN", "depth"]),
            ("0xE1000000", "st", "gDPHalf1", ["st"]),
        ]
        for w0, w1, name, args in cases:
            with self.subTest(name=name):
                self.assertEqual(gbi.decode(w0, w1, "f3dex2"), (name, args))

    def test_computed_fields_and_variant(self) -> None:
        name, args = gbi.decode(
            "_SHIFTL(0xFA, 24, 8) | _SHIFTL(minLevel, 8, 8) | _SHIFTL(lod, 0, 8)",
            "_SHIFTL(red, 24, 8) | _SHIFTL(green, 16, 8) | _SHIFTL(blue, 8, 8) | _SHIFTL(alpha, 0, 8)",
            "f3dex2",
        )
        self.assertEqual(name, "gDPSetPrimColor")
        self.assertEqual(args, ["minLevel", "lod", "red", "green", "blue", "alpha"])
        self.assertEqual(gbi.decode("0xBA001402", "0x100000", "f3dex"), ("gDPSetCycleType", ["G_CYC_2CYCLE"]))
        self.assertEqual(gbi.decode("0x06000000", "list", "f3dex"), ("gSPDisplayList", ["list"]))
        self.assertEqual(gbi.decode("0x01020040", "matrix", "f3dex"), ("gSPMatrix", ["matrix", "G_MTX_LOAD"]))
        self.assertEqual(gbi.decode("0xFF100000 | ((width - 1) & 0xFFF)", "image", "f3dex2")[1][2], "width")

    def test_negative_cases_are_verbatim_and_reported(self) -> None:
        cases = [
            ("0xAB000000", "0"),
            ("0xE7000001", "0"),
            ("0xE7000000", "1"),
            ("0xFA000000", "(r << 24) | (g << 16) | (b << 8)"),
            ("0xF6000000 | (x << 14)", "0"),
            ("word", "value"),
            ("_SHIFTL(0xFA,24,8)", "_SHIFTL(next(),24,8)"),
            ("0xD7000001", "0"),
            ("0xFC000000", "0x80000000"),
        ]
        for w0, w1 in cases:
            source = f"void f(void) {{ Gfx *p = dl++; p->words.w0 = {w0}; p->words.w1 = {w1}; }}"
            with self.subTest(w0=w0, w1=w1):
                result = gbi.lower(source, "f3dex2")
                self.assertEqual(result.source, source)
                self.assertEqual(len(result.raw), 1)
                self.assertFalse(result.macros)
        with self.assertRaises(Ambiguous):
            gbi.decode("0xE3000A01", "0", None)

    def test_source_shapes_comments_and_transparent_wrappers(self) -> None:
        source = """#define S(v,s,w) _SHIFTL(v,s,w)
#define WORD(a,b) { Gfx *g=dl++; g->w0=(a); g->w1=(b); }
void f(void) {
  { Gfx *p=dl++; p->words.w0=0xE7000000; /* retain */ p->words.w1=0; }
  indexed[i].words.w0=0xE6000000; indexed[i].words.w1=0;
  p->w0=0xE8000000; p->w1=0;
  WORD(S(0xFA,24,8), 0xFF0000FF);
  // p->words.w0=0xE7000000; p->words.w1=0;
}
"""
        result = gbi.lower(source, "f3dex2")
        self.assertEqual(sum(result.macros.values()), 4)
        self.assertIn("gDPPipeSync(dl++)", result.source)
        self.assertIn("gDPLoadSync(&indexed[i])", result.source)
        self.assertIn("gDPTileSync(p)", result.source)
        self.assertIn("gDPSetPrimColor", result.source)
        self.assertIn("/* retain */", result.source)
        self.assertIn("// p->words.w0=", result.source)
        self.assertNotIn("#define WORD", result.source)
        self.assertEqual(gbi.lower(result.source, "f3dex2").source, result.source)

    def test_custom_shiftl_is_not_assumed_standard(self) -> None:
        source = "#define _SHIFTL(v,s,w) ((v)+(s)+(w))\nvoid f(Gfx *p) {p->words.w0=0xE7000000;p->words.w1=0;}"
        result = gbi.lower(source, "f3dex2")
        self.assertEqual(result.source, source)
        self.assertIn("_SHIFTL", result.raw[0].reason)

    def test_static_forms(self) -> None:
        source = "Gfx list[] = {{{0xE7000000, 0}}, {{0xDF000000, 0}}, {{0xAB000000, 0}}};"
        result = gbi.lower(source, "f3dex2")
        self.assertEqual(result.macros, {"gsDPPipeSync": 1, "gsSPEndDisplayList": 1})
        self.assertIn("gsDPPipeSync()", result.source)
        self.assertIn("{{0xAB000000, 0}}", result.source)
        self.assertEqual(len(result.raw), 1)

    def test_volatile_control_and_directive_boundaries(self) -> None:
        cases = [
            "volatile Gfx *p; p=dl++; p->words.w0=0xE7000000; p->words.w1=0;",
            "Gfx *p; if (ready) p->words.w0=0xE7000000; p->words.w1=0;",
            "Gfx *p; p->words.w0=0xE7000000;\n#if ENABLE\np->words.w1=0;\n#endif",
            "list[i++].words.w0=0xE7000000; list[i++].words.w1=0;",
        ]
        for source in cases:
            with self.subTest(source=source):
                result = gbi.lower(source, "f3dex2")
                self.assertEqual(result.source, source)
                self.assertTrue(result.raw)
        source = "Gfx *p; p->words.w0=0xE7000000; // retain\np->words.w1=0;"
        self.assertIn("// retain\ngDPPipeSync(p);", gbi.lower(source, "f3dex2").source)

    def test_header_words_and_single_packet_evaluation(self) -> None:
        # C89 host execution independently verifies actual builders, including
        # static forms, against known hardware encodings and dynamic operands.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = root / "proof.c"
            code.write_text("""#include <stdio.h>
#define F3DEX_GBI_2
 typedef union { struct { unsigned int w0,w1; } words; double alignment; } Gfx;
#include "gbi.h"
Gfx fixed[] = {gsDPPipeSync(), gsDPSetCycleType(G_CYC_FILL),
 gsDPSetColorImage(G_IM_FMT_RGBA,G_IM_SIZ_16b,320,0), gsSPMatrix(0,G_MTX_LOAD),
 gsSP2Triangles(0,1,2,0,2,3,0,0),
 gsDPSetCombineLERP(0,0,0,PRIMITIVE,0,0,0,PRIMITIVE,0,0,0,PRIMITIVE,0,0,0,PRIMITIVE)};
int main(void) { Gfx commands[3], *p=commands; int x=7;
 gDPFillRectangle(p++,x,2,100,50);
 gDPSetPrimColor(p++,0,255,1,2,3,4);
 gSPTexture(p++,0x8000,0x8000,0,0,G_ON);
 printf("%ld\\n",(long)(p-commands));
 for(x=0;x<3;++x) printf("%08X %08X\\n",commands[x].words.w0,commands[x].words.w1);
 for(x=0;x<6;++x) printf("%08X %08X\\n",fixed[x].words.w0,fixed[x].words.w1);
 return 0; }
""")
            result = subprocess.run(
                [
                    "cc",
                    "-std=c89",
                    "-pedantic-errors",
                    "-I",
                    str(gbi.HEADER.parent),
                    str(code),
                    "-o",
                    str(root / "proof"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = subprocess.check_output([str(root / "proof")], text=True).splitlines()
            self.assertEqual(
                lines,
                [
                    "3",
                    "F61900C8 0001C008",
                    "FA0000FF 01020304",
                    "D7000002 80008000",
                    "E7000000 00000000",
                    "E3000A01 00300000",
                    "FF10013F 00000000",
                    "DA380003 00000000",
                    "06000204 00040600",
                    "FCFFFFFF FFFDF6FB",
                ],
            )

    def test_cli_rewrite_install_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = fixture(Path(directory))
            config = project.root / "config.toml"
            config.write_text(config.read_text().replace("cppflags=[]", 'cppflags=["-DF3DEX_GBI_2"]'))
            # This fixture is IDO; definitions must also be found in raw project
            # configuration, independent of the preprocessing recipe's family.
            code = project.src / "alpha.c"
            code.write_text(
                "typedef union { struct { unsigned int w0,w1; } words; } Gfx;\n"
                "void alpha(Gfx *p) {p->words.w0=0xE7000000;p->words.w1=0;}\n"
            )
            result = gbi.rewrite(project, [code])
            self.assertEqual(result["files_rewritten"], 1)
            self.assertEqual(result["macros"], {"gDPPipeSync": 1})
            self.assertTrue((project.include[0] / "gbi.h").is_file())
            self.assertIn('#include "gbi.h"', code.read_text())
            self.assertEqual(gbi.rewrite(project, [], all_files=True)["files_rewritten"], 0)
            json.dumps(result)
            with self.assertRaises(Held):
                gbi.rewrite(project, [])

    def test_draft_postpass(self) -> None:
        import sys

        from unbake.decomp import m2c

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, policy, _ = fixture(root)
            header = project.include[0] / "graphics.h"
            header.write_text("typedef union {struct {unsigned int w0,w1;} words;} Gfx;\nextern Gfx *dl;\n")
            tool = root / "m2c"
            tool.write_text(
                f"#!{sys.executable}\n"
                "print('void alpha(void) { Gfx *p=dl++; p->words.w0=0xE7000000; p->words.w1=0; }')\n"
            )
            tool.chmod(0o755)
            policy.m2c = tool
            draft = m2c.draft(project, cast(Policy, policy), "alpha", "us", project.work)
            self.assertIn("gDPPipeSync", draft.read_text())
            self.assertIn('#include "gbi.h"', draft.read_text())
