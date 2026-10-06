"""SDK-derived recovery and identity gates, with no external compiler execution."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.config import Held
from unbake.decomp import gbi_recover as recover

SDK = """
#define _SHIFTL(v,s,w) (((unsigned int)(v) & (0xFFFFFFFFU >> (32-(w)))) << (s))
#define CMD(pkt,a,b) { Gfx *g = (Gfx *)(pkt); g->words.w0=(a); g->words.w1=(b); }
#define G_SYNC 0xE7
#define gDPSync(pkt) CMD(pkt,_SHIFTL(G_SYNC,24,8),0)
#define gsDPSync() {{_SHIFTL(G_SYNC,24,8),0}}
#define gDPColor(pkt,r,g,b,a) CMD(pkt,0xFB000000,_SHIFTL(r,24,8)|_SHIFTL(g,16,8)|_SHIFTL(b,8,8)|_SHIFTL(a,0,8))
#define gDPRect(pkt,x,y,z,w) CMD(pkt,0xE4000000|_SHIFTL(z,12,12)|_SHIFTL(w,0,12),_SHIFTL(x,12,12)|_SHIFTL(y,0,12))
#define gSPAddress(pkt,a) CMD(pkt,0xDE000000,(unsigned int)(a))
#define gSPField(pkt,a,b) CMD(pkt,0xDA000000|_SHIFTL((a)-1,16,8)|_SHIFTL((b)*2,0,8),0)
#define gSPMode(pkt,s,n,d) CMD(pkt,0xE3000000|_SHIFTL(32-(s)-(n),8,8)|_SHIFTL((n)-1,0,8),(unsigned int)(d))
"""


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.patterns = recover.patterns(SDK)

    def test_table_per_field_family_and_non_gbi_preserved(self):
        cases = [
            ("0xE7000000", "0", "gDPSync(p)"),
            ("0xFB000000", "((r&255)<<24)|((g&255)<<16)|((b&255)<<8)|(a&255)", "gDPColor(p, r, g, b, a)"),
            (
                "0xE4000000|(((right*4)&4095)<<12)|((bottom*4)&4095)",
                "(((left*4)&4095)<<12)|((top*4)&4095)",
                "gDPRect(p, left*4, top*4, right*4, bottom*4)",
            ),
            ("0xDE000000", "(unsigned int)&other[i]", "gSPAddress(p, &other[i])"),
            ("0xDA030006", "0", "gSPField(p, 4, 3)"),
        ]
        for w0, w1, call in cases:
            with self.subTest(call=call):
                source = f"void f(Gfx *p) {{ int unchanged=17; p->words.w0={w0};p->words.w1={w1}; unchanged++; }}"
                result = recover.lower(source, self.patterns)
                self.assertIn(call + ";", result)
                self.assertIn("int unchanged=17;", result)
                self.assertTrue(result.endswith(" unchanged++; }"))

    def test_static_macro_from_header(self):
        source = "Gfx list[] = {{{0xE7000000,0}}};"
        self.assertEqual(recover.lower(source, self.patterns), "Gfx list[] = {gsDPSync()};")

    def test_unknown_mask_reserved_bits_and_order_refused(self):
        cases = [
            "p->words.w1=0;p->words.w0=0xE7000000;",
            "p->words.w0=0xE7000001;p->words.w1=0;",
            "p->words.w0=0xE7000000|unknown;p->words.w1=0;",
            "p->words.w0=0xE7000000;p->words.w1=1;",
            "p->words.w0=0x12345678;p->words.w1=0;",
            "p->words.w0=0xE4000000|((x&8191)<<12);p->words.w1=0;",
            "p->words.w0=0xE7000000; effect();p->words.w1=0;",
            "if(flag) p->words.w0=0xE7000000;p->words.w1=0;",
            "p->words.w0=0xE7000000;\n#if X\np->words.w1=0;\n#endif",
            "p->words.w0=0xE7000000;p->words.w1=call();",
        ]
        for source in cases:
            with self.subTest(source=source), self.assertRaisesRegex(Held, "first unmatched write"):
                recover.lower(source, self.patterns)

    def test_local_builder_uses_sdk_fields(self):
        source = (
            "#define EMIT(p,a,b) {Gfx *g=p;g->words.w0=a;g->words.w1=b;}\n"
            "void f(Gfx *p){EMIT(p,0xE7000000,0); other();}"
        )
        result = recover.lower(source, self.patterns)
        self.assertNotIn("#define EMIT", result)
        self.assertIn("gDPSync((p)); other();", result)
        with self.assertRaisesRegex(Held, "first unmatched write"):
            recover.lower(source.replace("0xE7000000", "0x12345678"), self.patterns)

    def test_local_sdk_imitation_refused(self):
        with self.assertRaisesRegex(Held, "first unmatched write"):
            recover.lower("#define gDPSync(pkt) bad(pkt)\ngDPSync(p);", self.patterns)

    def test_unrelated_code_and_reads_untouched(self):
        for source in [
            "p->value=17; other();",
            "if(p->words.w0==1) other();",
            'const char *s="p->words.w0=1;";',
            "/* p->words.w0=1; */",
        ]:
            self.assertEqual(recover.lower(source, self.patterns), source)

    def test_patterns_come_from_definition_not_macro_name(self):
        modified = recover.patterns(SDK.replace("0xE7", "0xAB").replace("gDPSync", "gDPInvented"))
        source = "p->words.w0=0xAB000000;p->words.w1=0;"
        self.assertIn("gDPInvented(p)", recover.lower(source, modified))
        with self.assertRaises(Held):
            recover.lower(source, self.patterns)

    def test_inline_word_emitter(self):
        source = (
            "static inline void emit(unsigned int a, unsigned int b) {"
            "Gfx *cmd; cmd = dl++; cmd->words.w0=a;cmd->words.w1=b;}\n"
            "void f(void){emit(0xE7000000,0); other();}"
        )
        result = recover.lower(source, self.patterns)
        self.assertNotIn("static inline", result)
        self.assertIn("gDPSync(dl++); other();", result)

    def test_local_wrapper(self):
        source = "#define gDPWait(p) gDPSync(p)\ngDPWait(dl++);"
        result = recover.lower(source, self.patterns)
        self.assertNotIn("#define", result)
        self.assertIn("gDPSync((dl++));", result)

    def test_shift_then_mask_uses_sdk_width(self):
        source = "p->words.w0=0xE4000000|((r<<12)&0xFFF000)|(b&4095);p->words.w1=((l<<12)&0xFFF000)|(t&4095);"
        self.assertIn("gDPRect(p, l, t, r, b)", recover.lower(source, self.patterns))

    def test_sdk_constants_and_correlated_fields(self):
        self.assertIn("gDPSync(p)", recover.lower("p->words.w0=G_SYNC<<24;p->words.w1=0;", self.patterns))
        self.assertIn("gSPMode(p, 19, 1, 0)", recover.lower("p->words.w0=0xE3000C00;p->words.w1=0;", self.patterns))

    def test_audio_packets_and_unmatched_inline_call_stay_held(self):
        for source in [
            "Acmd *p; p->words.w0=0xE7000000;p->words.w1=0;",
            "static inline void emit(int a,int b){Gfx *p=dl++;p->words.w0=a;p->words.w1=b;}"
            "void f(void){emit(unknown,0);}",
        ]:
            with self.subTest(source=source), self.assertRaises(Held):
                recover.lower(source, self.patterns)

    def test_existing_proof_checks_all_versions_modes_and_byte_order(self):
        from unbake.decomp import gbi_proof

        with tempfile.TemporaryDirectory() as temporary:
            project = SimpleNamespace(versions=("us", "eu"), root=Path(temporary) / "project")
            policy = SimpleNamespace(cache_machine_root=Path(temporary) / "cache")
            row = SimpleNamespace(aliases=("alpha",))
            with patch("unbake.layout.split.functions", return_value=[row]), patch.object(gbi_proof, "code") as code:
                code.return_value = ((".text", b"\x01\x02", ()),)
                gbi_proof.preserve(project, policy, Path("alpha.c"), "before", "after")
                self.assertEqual(
                    [(c.args[2], c.args[4]) for c in code.call_args_list],
                    [("us", 0), ("us", 0), ("us", 1), ("us", 1), ("eu", 0), ("eu", 0), ("eu", 1), ("eu", 1)],
                )
                for change in [b"\x02\x01", b"\x01\x03", b"\x01\x02\x03"]:
                    code.side_effect = [((".text", b"\x01\x02", ()),), ((".text", change, ()),)]
                    with self.subTest(change=change), self.assertRaisesRegex(Held, "changes codegen"):
                        gbi_proof.preserve(project, policy, Path("alpha.c"), "before", "after")

    def test_equivalent_evidence_sdk_and_scalar_include_aliases(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            old, live = root / "old", root / "include"
            old.mkdir()
            live.mkdir()
            (old / "legacy_commands.h").write_text(SDK)
            (old / "legacy_scalars.h").write_text("typedef int s32;\n")
            project = SimpleNamespace(include=(live,), declaration_evidence=(old,))
            headers = {live / "commands.h": SDK, live / "types.h": "typedef signed int s32;\n"}
            source = '#include "legacy_commands.h"\n#include "legacy_scalars.h"\np->words.w0=0xE7000000;p->words.w1=0;'
            after = recover.import_aliases(project, source, headers)
            self.assertIn('#include "commands.h"', after)
            self.assertIn('#include "types.h"', after)
            ordinary = '#include "legacy_scalars.h"\nint value=17;'
            self.assertEqual(recover.import_aliases(project, ordinary, headers), ordinary)
            (old / "legacy_scalars.h").write_text("typedef unsigned int s32;\n")
            self.assertIn('"legacy_scalars.h"', recover.import_aliases(project, source, headers))

    def test_selector_parameter_is_not_a_command_recovery(self):
        generic = "#define gSPBits(pkt,cmd,data) CMD(pkt,_SHIFTL(cmd,24,8),(unsigned int)(data))\n"
        self.assertNotIn("gSPBits", [pattern.macro.name for pattern in recover.patterns(SDK + generic)])
        with self.assertRaises(Held):
            recover.lower("p->words.w0=0xAB000000;p->words.w1=0;", recover.patterns(SDK + generic))

    def test_full_word_projection_and_packet_single_evaluation(self):
        source = "p->words.w0=0xFB000000;p->words.w1=color;"
        after = recover.lower(source, self.patterns)
        self.assertIn("gDPColor(p,", after)
        self.assertIn(" >> 24", after)
        self.assertIn(" >> 0", after)
        source = "#define EMIT(p,a,b) {Gfx *g=p;g->words.w0=a;g->words.w1=b;}\nvoid f(void){EMIT(dl++,0xE7000000,0);}"
        self.assertEqual(recover.lower(source, self.patterns).count("dl++"), 1)

    def test_chained_literal_sdk_selector_arithmetic(self):
        self.assertEqual(recover.literal("32 - (19) - (1)"), 12)
        self.assertEqual(recover.literal("7 / 2 / 1"), 3)
        self.assertIsNone(recover.literal("selector - 1 - 1"))
        derived = recover.patterns(SDK + "#define gDPFixedMode(pkt,d) CMD(pkt,0xE3000000|_SHIFTL(32-19-1,8,8),d)\n")
        self.assertIn("gDPFixedMode(p, 0)", recover.lower("p->words.w0=0xE3000C00;p->words.w1=0;", derived))

    def test_table_of_installed_sdk_macro_families(self):
        from tests.decomp.test_gbi_held import HeldPacketsTest
        from unbake.decomp import gbi
        from unbake.decomp.gbi_source import macros

        definitions = HeldPacketsTest().definitions("f3dex2")
        definitions["_GBI_CMD"] = macros(gbi.HEADER.read_text())["_GBI_CMD"]
        constants = {
            "G_VTX": 1,
            "G_MOVEMEM": 0xDC,
            "G_MOVEWORD": 0xDB,
            "G_TEXTURE": 0xD7,
            "G_SETTIMG": 0xFD,
            "G_FILLRECT": 0xF6,
            "G_SETPRIMDEPTH": 0xEE,
            "G_SETTILE": 0xF5,
            "G_SETSCISSOR": 0xED,
        }
        sdk = (
            "\n".join(
                f"#define {macro.name}({','.join(macro.parameters)}) {macro.body}" for macro in definitions.values()
            )
            + "\n"
            + "\n".join(f"#define {name} {value}" for name, value in constants.items())
        )
        catalogue = recover.patterns(sdk)
        cases = [
            ("gSPMoveWord", "0xDB060010", "address", "6, 16, address"),
            ("gSPMoveMem", "0xDC10020A", "address", "10, 16, 17, address"),
            ("gSPVertex", "0x0100400C", "address", "address, 4, 2"),
            ("gSPTexture", "0xD7001302", "0x00100020", "16, 32, 2, 3, 1"),
            ("gDPSetTextureImage", "0xFD90003F", "address", "4, 2, 64, address"),
            ("gDPFillRectangle", "0xF60A0140", "0x00010020", "4, 8, 40, 80"),
            ("gDPSetPrimDepth", "0xEE000000", "0x006400C8", "100, 200"),
            ("gDPSetScissorFrac", "0xED004008", "0x00028050", "0, 4, 8, 40, 80"),
        ]
        for name, w0, w1, arguments in cases:
            with self.subTest(family=name):
                family = [pattern for pattern in catalogue if pattern.macro.name == name]
                self.assertEqual(len(family), 1)
                source = f"p->words.w0={w0};p->words.w1={w1};"
                self.assertEqual(recover.lower(source, family), f"{name}(p, {arguments});")
