"""Guard diagnostics, explicit exceptions, and balanced source syntax."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from unbake.decomp import checks
from unbake.project.config import Held


class ChecksTest(unittest.TestCase):
    def test_refusals_name_rule_and_original_line(self) -> None:
        cases = [
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
                    checks.resolve(findings, None, None)

    def test_real_c_forms_avoid_false_positives(self) -> None:
        cases = [
            "",
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

    def test_marked_exception_is_accepted_and_serializable(self) -> None:
        content = "/* FAKEMATCH: preserve the measured scheduling effect. */\nvoid f(void) { do {} while (0); }"
        findings = checks.run(content)
        self.assertEqual(checks.resolve(findings, None, None), [])
        reason = "preserve the measured scheduling effect."
        self.assertEqual(findings[0].fakematch, reason)
        receipt = json.loads(json.dumps({"function": "f", "fakematch": checks.fakematches(content)}))
        self.assertEqual(receipt["fakematch"], [reason])
        self.assertEqual(checks.fakematches('const char *s = "/* FAKEMATCH: text */";'), ())

    def test_missing_values_are_named(self) -> None:
        cases = [
            (lambda: checks.run(None), "source"),
            (lambda: checks.fakematches(None), "source_text"),
            (lambda: checks.run("/* FAKEMATCH: */"), "FAKEMATCH.reason"),
            (lambda: checks.resolve([None], None, None), "GuardFinding"),
            (lambda: checks.resolve(None, None, None), "findings"),
            (lambda: checks.derive(None), "trial_context.source"),
        ]
        for operation, name in cases:
            with self.subTest(name=name), self.assertRaisesRegex(Held, name):
                operation()
        with (
            tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary,
            self.assertRaisesRegex(Held, "source"),
        ):
            checks.run(Path(temporary) / "missing.c")

    def test_file_input_and_nested_directives(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            path = Path(temporary) / "f.c"
            path.write_text("#ifdef VERSION_US\n#if DEBUG\nint x;\n#endif\n#endif\n")
            self.assertEqual(checks.run(path)[0].rule, "file-version-guard")
