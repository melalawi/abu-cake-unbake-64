"""Guard diagnostics, explicit exceptions, and balanced source syntax."""

import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unbake.config import Held
from unbake.decomp import checks


class ChecksTest(unittest.TestCase):
    def test_refusals_name_rule_and_original_line(self) -> None:
        cases = [
            ("gfx", 1, "p->words.w0 = 0xE7000000;", "raw-gfx"),
            ("macro", 1, "#define _SHIFTL(a,b,c) (a)", "local-gbi-macro"),
            ("type", 1, "typedef int s32;", "local-type-copy"),
            ("gfx type", 1, "typedef union { struct { int w0, w1; } words; double align; } Gfx;", "local-type-copy"),
            ("static macro", 1, "#define gsDPSetColor(x) (x)", "local-gbi-macro"),
            ("struct", 1, "struct func_80201234_S1 { int value; };", "invented-struct"),
            ("symbol", 1, "#define global ((int*)0x80201234)", "symbol-alias"),
            ("func_80220EB0", 1, 'void f(void) { __asm__("nop"); }', "inline-asm"),
            ("func_80234FDC", 575, "x = *(s32*)((char*)p + 0x18);", "raw-offset"),
            ("func_802ACBCC", 375, "x = *((s32*)((u8*)p + 0x3));", "raw-offset"),
            ("func_802ACBCC", 387, "x = *(f32*)((char*)p + 387);", "raw-offset"),
            ("storage", 1, "volatile int x;", "volatile-storage"),
            ("include", 1, '#include "../drafts/thing.h"', "local-include"),
            ("comment", 1, "/* objdiff forced this statement */", "tool-comment"),
            ("version", 1, "#ifdef VERSION_US\nvoid f(void) {}\n#endif", "file-version-guard"),
            ("empty", 1, "void f(void) { do {} while (0); }", "empty-loop"),
        ]
        for function, line, content, rule in cases:
            with self.subTest(function=function, line=line):
                findings = checks.run("\n" * (line - 1) + content)
                self.assertTrue(findings)
                self.assertEqual((findings[0].rule, findings[0].line), (rule, line))
                with self.assertRaisesRegex(Held, rule):
                    checks.resolve(list(findings), None, None)

    def test_real_c_forms_avoid_false_positives(self) -> None:
        cases = [
            "",
            "p->words.w0 = raw; /* GBI_RAW: dynamic opcode cannot be decoded */",
            "typedef int SharedDomain;",
            '/* asm volatile */ void f(void) { puts("__asm__"); }',
            "int x = *(volatile int *)&p->field;",
            "int x = *(Entry * volatile *)&p->field;",
            "x = array[3]; y = p->field; z = (int*)p;",
            '#include "types.h"\nint x;',
            "void f(void) {\n#ifdef VERSION_US\nx = 1;\n#else\nx = 2;\n#endif\n}",
            "#ifdef NON_MATCHING\nvoid f(void) {}\n#endif",
            "void f(void) { do { x++; } while (0); }",
        ]
        for content in cases:
            with self.subTest(content=content):
                self.assertEqual(checks.run(content), [])

    def test_raw_offsets_are_bounded_unary_addresses(self) -> None:
        cases = [
            ("value arithmetic", "x = *(int *)(array + index) + 1;", 0),
            ("value comparison", "if (*(int *)(array + index) != -1) {}", 0),
            ("multiline value", "x = *(int *)(array + index)\n + 1;", 0),
            ("multiplication", "x = factor * ((int *)(array + 4));", 0),
            ("multiplication with load", "x = factor * (*((int *)(array + 4)));", 1),
            ("nested char buffer", "x = *(int *)(p->buffer + 0x1C);", 1),
            ("negative field", "x = *(int *)((char *)p - 4);", 1),
            ("parenthesized field", "x = *(int *)((char *)p + (0x18));", 1),
            ("zero base reinterpretation", "x = *(int *)((char *)p + (0));", 0),
            ("nested field loads", "x = *(int *)(*(char **)(p + 4) + 8);", 2),
            ("dynamic outer address", "x = *(int *)(p + *(int *)(q + 4));", 1),
            ("outer scalar cast", "x = (u32)*(s32 *)(p + 0x18);", 1),
            ("indexed object field", "x = *(int *)((char *)p + index * 0x18 + 0xA4);", 1),
            ("function pointer field", "x = *((Callback (**)(void *))((char *)p + 4));", 1),
            ("scalar reinterpretation", "x = *((u16 *)&arg + 1);", 1),
            ("multiple loads", "x = *(int *)(p + 4) + *(int *)(p + 8);", 2),
        ]
        for pattern, content, count in cases:
            with self.subTest(pattern=pattern):
                findings = [finding for finding in checks.run(content) if finding.rule == "raw-offset"]
                self.assertEqual(len(findings), count)
                if count:
                    with self.assertRaisesRegex(Held, "raw-offset"):
                        checks.resolve(list(findings), None, None)

    def test_generic_scalar_stores_are_not_display_packets(self) -> None:
        cases = [
            "player->fxStage = -1; player->fxTimer = 0;",
            "bot->route = -2; bot->routePos = -1;",
            "screen->selection = -1; screen->state = 3;",
            "p->first = (s32)(-1); p->second = address;",
            "p->first = ~0; p->second = 0;",
            "p->first = -256 | (index << 8); p->second = address;",
            "p->first = 0; p->second = 22;",
            "p->first = 0x12345678; p->second = address;",
            "magic = 0xB8000000; p->unk0 = -1; p->unk4 = -2; p = &p->unk8;",
            "if (p->first == 0xBF000000) { p->second = 0; }",
        ]
        for content in cases:
            with self.subTest(content=content):
                self.assertEqual([f for f in checks.run(content) if f.rule == "raw-gfx"], [])

    def test_display_packet_stores_remain_refused(self) -> None:
        cases = [
            "p->words.w0 = runtime;",
            "p->words_w1 = address;",
            "p->w0 = runtime; p->w1 = address;",
            "Gfx *p; p->first = runtime; p->second = address;",
            "p->unk0 = (s32)(((n & 0xFF) << 0x10) | 0x01000040); p->unk4 = address;",
            "p->unk0 = 0xBF000000; p->unk4 = vertices;",
            "p->first = 0xFF100000; p->second = address;",
            "p->first = 0xE7 << 24; p->second = 0;",
            "p->first = _SHIFTL(0xE7, 24, 8); p->second = 0;",
            "magic = 0xB8000000; p->unk0 = replacement0; p->unk4 = replacement4; p = &p->unk8;",
        ]
        for content in cases:
            with self.subTest(content=content):
                self.assertTrue([f for f in checks.run(content) if f.rule == "raw-gfx"])

    def test_volatile_distinguishes_storage_from_type_and_device_access(self) -> None:
        cases = [
            ("parenthesized cast", "x = *((volatile float *) (&p->field));", 0),
            ("extra parentheses", "x = *(((volatile float *) (&p->field)));", 0),
            ("sizeof type", "char pad[4 - sizeof(volatile int)];", 0),
            ("sizeof pointer", "char pad[8 - sizeof(volatile Entry *)];", 0),
            ("alignment type", "x = _Alignof(volatile int);", 0),
            ("return qualifier", "volatile unsigned long long f(void) {}", 0),
            ("device cast", "p = (volatile u32 *)0xA4600010;", 0),
            (
                "device alias",
                "void f(void) { volatile u32 *p; volatile u32 *q; "
                "p = (volatile u32 *)0xA4600010; q = p; while (*q) {} }",
                0,
            ),
            ("device initializer", "void f(void) { volatile u32 *p = (volatile u32 *)0xA4600010; *p = 2; }", 0),
            (
                "nested device alias uses declaration scope",
                "void f(void) { volatile u32 *p; volatile u32 *q; "
                "p=(volatile u32 *)0xA4600010; if (*p) { q=p; while (*q) {} } }",
                0,
            ),
            (
                "plain shadow cannot qualify outer pointer",
                "void f(void) { volatile u32 *p=(volatile u32 *)0xA4600010; volatile u32 *q; "
                "if (*p) { u32 *q; q=p; } }",
                1,
            ),
            (
                "ordinary reassignment invalidates alias",
                "void f(void) { volatile u32 *p=(volatile u32 *)0xA4600010; volatile u32 *q; q=p; p=memory; }",
                2,
            ),
            (
                "ordinary field assignment is not pointer reassignment",
                "void f(void) { volatile u32 *p=(volatile u32 *)0xA4600010; object->p=memory; }",
                0,
            ),
            ("local dead stores", "void f(void) { volatile int ret; ret = 1; }", 1),
            ("volatile field", "struct S { volatile int field; };", 1),
            ("aggregate scheduling cast", "state = ((volatile struct Screen *)p)->rows[row].state;", 1),
            ("unknown pointer storage", "void f(void) { volatile int *p; }", 1),
            ("volatile pointer itself", "void f(void) { u32 *volatile p = (u32 *)0xA4600010; }", 1),
            ("ordinary memory cast", "p = (volatile u32 *)0x80000000;", 1),
            (
                "device name in another function",
                "void f(void) { volatile u32 *p = (volatile u32 *)0xA4600010; } void g(void) { volatile u32 *p; }",
                1,
            ),
            ("marked real storage", "/* FAKEMATCH: measured scheduling */ volatile int x;", 1),
        ]
        for pattern, content, count in cases:
            with self.subTest(pattern=pattern):
                findings = [finding for finding in checks.run(content) if finding.rule == "volatile-storage"]
                self.assertEqual(len(findings), count)
                if count and "FAKEMATCH" not in content:
                    with self.assertRaisesRegex(Held, "volatile-storage"):
                        checks.resolve(list(findings), None, None)
                elif "FAKEMATCH" in content:
                    self.assertEqual(checks.resolve(list(findings), None, None), [])

    def test_marked_exception_is_accepted_and_serializable(self) -> None:
        content = "/* FAKEMATCH: preserve the measured scheduling effect. */\nvoid f(void) { do {} while (0); }"
        findings = checks.run(content)
        self.assertEqual(checks.resolve(list(findings), None, None), [])
        reason = "preserve the measured scheduling effect."
        self.assertEqual(findings[0].fakematch, reason)
        receipt = json.loads(json.dumps({"function": "f", "fakematch": checks.fakematches(content)}))
        self.assertEqual(receipt["fakematch"], [reason])
        self.assertEqual(checks.fakematches('const char *s = "/* FAKEMATCH: text */";'), ())

    def test_missing_values_are_named(self) -> None:
        invalid: Any = None
        cases: list[tuple[Callable[[], object], str]] = [
            (lambda: checks.run(invalid), "source"),
            (lambda: checks.fakematches(invalid), "source_text"),
            (lambda: checks.run("/* FAKEMATCH: */"), "FAKEMATCH.reason"),
            (lambda: checks.resolve([invalid], None, None), "GuardFinding"),
            (lambda: checks.resolve(invalid, None, None), "findings"),
        ]
        for operation, name in cases:
            with self.subTest(name=name), self.assertRaisesRegex(Held, name):
                operation()
        with (
            tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary,
            self.assertRaisesRegex(Held, "source"),
        ):
            checks.run(Path(temporary).resolve() / "missing.c")

    def test_file_input_and_nested_directives(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            path = Path(temporary).resolve() / "f.c"
            path.write_text("#ifdef VERSION_US\n#if DEBUG\nint x;\n#endif\n#endif\n")
            self.assertEqual(checks.run(path)[0].rule, "file-version-guard")
