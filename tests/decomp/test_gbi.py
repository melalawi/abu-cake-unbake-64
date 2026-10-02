"""GBI recovery, conservative failures, and independent command-word proofs."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

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

    def test_audio_definitions_require_symbolic_equivalence(self) -> None:
        from unbake.decomp import gbi_audio

        source = gbi_audio.HEADER.read_text()
        result = gbi.lower(source)
        self.assertNotIn("#define aSetBuffer", result.source)
        self.assertNotIn("#define aSetVolume", result.source)
        self.assertEqual(result.headers, {"abi"})
        changed = source.replace("_SHIFTL(f,16,16)", "_SHIFTL(f,16,8)")
        result = gbi.lower(changed)
        self.assertIn("#define aSetVolume", result.source)
        self.assertTrue(any(item.command == "aSetVolume builder" for item in result.raw))

    def test_audio_pointer_alias_and_side_effectful_packet(self) -> None:
        source = "typedef Acmd Audio; void f(Audio *p) {p->words.w0=7<<24;p->words.w1=0;}"
        result = gbi.lower(source)
        self.assertEqual(result.macros, {"aSegment": 1})
        self.assertIn("aSegment(p, 0, 0);", result.source)
        source = "void f(Acmd *p) { { Acmd *a=p++; a->words.w0=4<<24;a->words.w1=address; } }"
        result = gbi.lower(source)
        self.assertIn("aLoadBuffer(p++, address);", result.source)

    def test_sdk_alias_and_flattened_words_are_not_invisible(self) -> None:
        source = "typedef Gfx Display; void f(Display *p) {p->words.w0=0xE7000000;p->words.w1=0;}"
        self.assertIn("gDPPipeSync(p)", gbi.lower(source, "f3dex2").source)
        source = "typedef Gfx Display; Display list[]={{{0xE7000000,0}}};"
        self.assertIn("gsDPPipeSync()", gbi.lower(source, "f3dex2").source)
        source = (
            "typedef Gfx Display;\n"
            "#define SYNC(pkt) {Display *p=pkt;p->words.w0=0xE7000000;p->words.w1=0;}\nSYNC(dl++);"
        )
        self.assertIn("gDPPipeSync(dl++)", gbi.lower(source, "f3dex2").source)
        source = "typedef Shared_Gfx Gfx; void f(Gfx *p) {p->words_w0=word;p->words_w1=value;}"
        result = gbi.lower(source, "f3dex2")
        self.assertEqual(result.source, source)
        self.assertEqual(len(result.raw), 1)
        self.assertTrue(result.raw[0].reason)

    def test_trailing_w1_is_reported(self) -> None:
        source = "void f(Gfx *p) {p->words.w1=payload;}"
        result = gbi.lower(source, "f3dex2")
        self.assertEqual(result.source, source)
        self.assertEqual(result.raw[0].command, "unpaired word write")

    def test_unconditional_opcode_macros_and_conditional_refusal(self) -> None:
        body = "void f(Gfx *p) {p->words.w0=_SHIFTL(OP,24,8);p->words.w1=0;}"
        self.assertIn("gDPPipeSync", gbi.lower("#define OP 0xE7\n" + body, "f3dex2").source)
        source = "#if MODE\n#define OP 0xE7\n#endif\n" + body
        self.assertEqual(gbi.lower(source, "f3dex2").source, source)
        for directive in ("#undef OP", "#if MODE\n#define OP 0xE6\n#endif"):
            source = "#define OP 0xE7\n" + directive + "\n" + body
            self.assertEqual(gbi.lower(source, "f3dex2").source, source)

    def test_intervening_local_assignments_preserve_dependencies(self) -> None:
        text = "void f(Gfx *p) { p->words.w0=0xE7000000; next=old+1; p->words.w1=0; }"
        result = gbi.lower(text, "f3dex2")
        self.assertEqual(result.macros, {"gDPPipeSync": 1})
        self.assertLess(result.source.index("next=old+1;"), result.source.index("gDPPipeSync"))
        for assignment in ("color=7", "next=read()", "next=*input", "next=input[0]"):
            text = f"void f(Gfx *p) {{ p->words.w0=0xF7000000; {assignment}; p->words.w1=color; }}"
            # Assigning a w1 dependency is safe, but reading aliased memory or
            # calling a function between stores must remain in original order.
            result = gbi.lower(text, "f3dex2")
            if assignment == "color=7":
                self.assertIn("gDPSetFillColor", result.source)
            else:
                self.assertEqual(result.source, text)
        text = "void f(Gfx *p) {p->words.w0=0xF7000000 | color; color=7; p->words.w1=0;}"
        self.assertEqual(gbi.lower(text, "f3dex2").source, text)
        text = "void f(Gfx *p) {p->words.w1=0; p->words.w0=0xE7000000;}"
        self.assertEqual(gbi.lower(text, "f3dex2").macros, {"gDPPipeSync": 1})
        text = "void f(Gfx *p) {p->words.w0=0xE7000000; p->words.w1=0; p->words.w0=0xE6000000;}"
        result = gbi.lower(text, "f3dex2")
        self.assertEqual(result.macros, {"gDPPipeSync": 1})
        self.assertEqual(len(result.raw), 1)

    def test_codegen_refusal_keeps_original_source_and_names_function(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, policy, _ = fixture(Path(directory))
            code = project.src / "alpha.c"
            original = "typedef struct {unsigned w0,w1;} Gfx; void alpha(Gfx *p) {p->w0=0xE7000000;p->w1=0;}"
            code.write_text(original)
            with patch("unbake.decomp.gbi_proof.preserve", side_effect=Held("gbi", "alpha: rewrite changes codegen")):
                result = gbi.rewrite(project, cast(Policy, policy), [code])
            self.assertEqual(code.read_text(), original)
            self.assertEqual(result["files_rewritten"], 0)
            self.assertEqual(result["macros"], {})
            self.assertIn("alpha:", str(result["raw"]))

    def test_install_does_not_replace_a_project_gbi_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = fixture(Path(directory))
            existing = project.include[0] / "gbi.h"
            existing.write_text("/* Project-owned graphics declarations. */\n")
            with self.assertRaisesRegex(Held, "existing header differs"):
                gbi.install(project)
            self.assertEqual(existing.read_text(), "/* Project-owned graphics declarations. */\n")

    def test_audio_install_preserves_project_abi_and_refuses_owned_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = fixture(Path(directory))
            existing = project.include[0] / "abi.h"
            existing.write_text("/* Project-owned audio declarations. */\n")
            self.assertEqual(gbi.install_audio(project), '#include "shared/abi.h"\n')
            self.assertEqual(existing.read_text(), "/* Project-owned audio declarations. */\n")
            (project.include[0] / "shared/acmd.h").write_text("/* Owned type */\n")
            with self.assertRaises(Held):
                gbi.install_audio(project)

    def test_type_only_cleanup_and_scalar_header_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, policy, _ = fixture(Path(directory))
            root = project.include[0]
            (root / "basetypes.h").write_text(
                "typedef unsigned char u8; typedef unsigned short u16; typedef unsigned int u32; "
                "typedef int s32; typedef long long s64; typedef float f32;\n"
            )
            (root / "n64sdk.h").write_text("typedef union {struct {u32 w0,w1;} words;s64 alignment;} Gfx;\n")
            code = project.src / "alpha.c"
            code.write_text(
                '#include "basetypes.h"\ntypedef struct {u32 w0;u32 w1;} Gfx;\nvoid alpha(Gfx *p) {p->w1=payload;}\n'
            )
            with patch("unbake.decomp.gbi_proof.preserve"):
                result = gbi.rewrite(project, cast(Policy, policy), [code])
            text = code.read_text()
            self.assertEqual(result["files_rewritten"], 1)
            self.assertNotIn("typedef struct", text)
            self.assertIn("p->words.w1=payload", text)
            self.assertLess(text.index('"basetypes.h"'), text.index('"n64sdk.h"'))
            self.assertTrue(result["raw"])

    def test_shared_packet_storage_alias_is_lowered_and_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, policy, _ = fixture(Path(directory))
            root = project.include[0]
            (root / "basetypes.h").write_text(
                "typedef unsigned char u8; typedef unsigned short u16; typedef unsigned int u32; "
                "typedef int s32; typedef long long s64; typedef float f32;\n"
            )
            (root / "n64sdk.h").write_text("typedef union {struct {u32 w0,w1;} words;s64 alignment;} Gfx;\n")
            (root / "shared").mkdir()
            (root / "shared/gfx.h").write_text(
                "typedef struct Shared_Gfx Shared_Gfx;struct Shared_Gfx {u32 words_w0;u32 words_w1;};\n"
            )
            code = project.src / "alpha.c"
            code.write_text("typedef Shared_Gfx Gfx; void alpha(Gfx *p) {p->words_w0=0xE7000000;p->words_w1=0;}")
            with patch("unbake.decomp.gbi_proof.preserve"):
                result = gbi.rewrite(project, cast(Policy, policy), [code])
            self.assertEqual(result["macros"], {"gDPPipeSync": 1})
            self.assertNotIn("typedef Shared_Gfx", code.read_text())
            self.assertIn("gDPPipeSync(p)", code.read_text())

    def test_audio_header_encodings_and_packet_evaluation(self) -> None:
        from unbake.decomp import gbi_audio

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = root / "proof.c"
            code.write_text("""#include <stdio.h>
typedef union {struct {unsigned int w0,w1;} words;double alignment;} Acmd;
#include "abi.h"
int main(void) { Acmd commands[15], *p=commands; int i;
 aADPCMdec(p++,1,0x12345678); aClearBuffer(p++,0x123,0x456);
 aEnvMixer(p++,2,0x12345678); aLoadBuffer(p++,0x12345678);
 aResample(p++,3,0x4567,0x12345678); aSaveBuffer(p++,0x12345678);
 aSegment(p++,2,0x123456); aSetBuffer(p++,4,0x1234,0x5678,0x9ABC);
 aSetVolume(p++,0x1234,0x5678,0x9ABC,0xDEF0); aDMEMMove(p++,0x123456,0x789A,0xBCDE);
 aLoadADPCM(p++,0x123456,0x12345678); aMix(p++,5,0x1234,0x5678,0x9ABC);
 aInterleave(p++,0x1234,0x5678); aPoleFilter(p++,6,0x1234,0x12345678);
 aSetLoop(p++,0x12345678);
 printf("%ld\\n", (long)(p-commands));
 for(i=0;i<15;i++) printf("%08X %08X\\n",commands[i].words.w0,commands[i].words.w1);
 return 0; }
""")
            binary = root / "proof"
            subprocess.run(
                [
                    "cc",
                    "-std=c89",
                    "-pedantic-errors",
                    "-I",
                    str(gbi_audio.HEADER.parent),
                    str(code),
                    "-o",
                    str(binary),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            output = subprocess.check_output([str(binary)], text=True).splitlines()
            self.assertEqual(
                output,
                [
                    "15",
                    "01010000 12345678",
                    "02000123 00000456",
                    "03020000 12345678",
                    "04000000 12345678",
                    "05034567 12345678",
                    "06000000 12345678",
                    "07000000 02123456",
                    "08041234 56789ABC",
                    "1B345678 9ABCDEF0",
                    "0A123456 789ABCDE",
                    "0B123456 12345678",
                    "0C051234 56789ABC",
                    "0D000000 12345678",
                    "0E061234 12345678",
                    "0F000000 12345678",
                ],
            )

    def test_failed_pointer_folding_retries_without_moving_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, policy, _ = fixture(Path(directory))
            code = project.src / "alpha.c"
            code.write_text(
                "typedef struct {unsigned w0,w1;} Gfx; extern Gfx *dl;\n"
                "void alpha(void) { Gfx *p; p=dl++; p->w0=0xE7000000; p->w1=0; }\n"
            )
            with patch("unbake.decomp.gbi_proof.preserve", side_effect=[Held("gbi", "alpha: changed codegen"), None]):
                result = gbi.rewrite(project, cast(Policy, policy), [code])
            self.assertEqual(result["files_rewritten"], 1)
            self.assertIn("p=dl++;", code.read_text())
            self.assertIn("gDPPipeSync(p)", code.read_text())

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
            project, policy, _ = fixture(Path(directory))
            config = project.root / "config.toml"
            config.write_text(config.read_text().replace("cppflags=[]", 'cppflags=["-DF3DEX_GBI_2"]'))
            # This fixture is IDO; definitions must also be found in raw project
            # configuration, independent of the preprocessing recipe's family.
            code = project.src / "alpha.c"
            code.write_text(
                "typedef union { struct { unsigned int w0,w1; } words; } Gfx;\n"
                "void alpha(Gfx *p) {p->words.w0=0xE7000000;p->words.w1=0;}\n"
            )
            with patch("unbake.decomp.gbi_proof.preserve") as proof:
                result = gbi.rewrite(project, cast(Policy, policy), [code])
                proof.assert_called_once()
            self.assertEqual(result["files_rewritten"], 1)
            self.assertEqual(result["macros"], {"gDPPipeSync": 1})
            self.assertTrue((project.include[0] / "gbi.h").is_file())
            self.assertIn('#include "gbi.h"', code.read_text())
            self.assertEqual(gbi.rewrite(project, cast(Policy, policy), [], all_files=True)["files_rewritten"], 0)
            json.dumps(result)
            with self.assertRaises(Held):
                gbi.rewrite(project, cast(Policy, policy), [])

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
