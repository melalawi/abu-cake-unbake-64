"""Linked relocation identity and guarded source context regressions."""

import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import assemble
from tests.support import test_policy
from unbake.decomp.score import diff
from unbake.decomp.trial_compare import compare_object
from unbake.decomp.trial_source import control_context


class RelocationTargetTests(unittest.TestCase):
    def setUp(self):
        from tests.objdiff_fixture import install

        install(self)

    def test_final_jump_table_and_branch_targets_keep_real_differences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            generation = root / "build/us.0"
            generation.mkdir(parents=True)
            policy = test_policy(root)
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "lui $v0,%hi({table})\nlw {register},%lo({table})($v0)\n"
                "bne $a0,$v0,{end}{branch}\nnop\n{global_}{end}:\njr $ra\nnop\n"
                ".size alpha,.-alpha\n{data}"
            )
            table_data = ".section .rdata\n.word .Lend{entry}, alpha\n"
            baseline = assemble(
                root,
                "baseline",
                body.format(
                    table=".rdata",
                    register="$v0",
                    end=".Lend",
                    branch="",
                    global_="",
                    data=table_data.format(entry=""),
                ),
            )
            script = root / "link.ld"
            script.write_text("SECTIONS { .text 0x80400000 : { *(.text) } .rdata 0x800EFFF0 : { *(.rdata) } }\n")
            from tests.elf_fixture import linked_fixture

            linked_fixture(generation / "game.elf", [baseline], {".text": 0x80400000, ".rdata": 0x800EFFF0})
            (generation / "game.map").write_text(
                " .text 0x80400000 0x18 obj/src/alpha.o\n .rdata 0x800EFFF0 0x8 obj/src/alpha.o\n"
                " 0x800EFFF0 jump_table\n 0x80400000 alpha\n"
            )
            target = assemble(
                root,
                "target",
                body.format(
                    table="jump_table",
                    register="$v0",
                    end="target_end",
                    branch="",
                    global_=".globl target_end\n",
                    data="",
                ),
            )
            for register, branch, table, entry in (
                ("$v0", "", ".rdata", ""),
                ("$v1", "", ".rdata", ""),
                ("$v0", "+4", ".rdata", ""),
                ("$v0", "", ".rdata+4", ""),
                ("$v0", "", ".rdata", "+4"),
                ("$v0", "", ".rdata", "removed"),
            ):
                with self.subTest(register=register, branch=branch, table=table, entry=entry):
                    candidate = assemble(
                        root,
                        "candidate",
                        body.format(
                            table=table,
                            register=register,
                            end=".Lend",
                            branch=branch,
                            global_="",
                            data=".section .rdata\n.word 0, 0\n"
                            if entry == "removed"
                            else table_data.format(entry=entry),
                        ),
                    )
                    result = compare_object(
                        "us",
                        diff(policy, "us", "alpha", target, candidate, root / "diff.json", generation=generation),
                        "alpha",
                    )
                    changed = register != "$v0" or bool(branch) or table != ".rdata" or bool(entry)
                    self.assertEqual(result.identical == result.of and not any(result.typed.values()), not changed)
                    if entry:
                        self.assertIn("jump table", "\n".join(result.lines))

    def test_guarded_braces_rejoin_and_truncated_controls_do_not_crash(self) -> None:
        source = "void alpha(int x) {\n#if PAL\nif (x) {\n#else\nif (x > 1) {\n#endif\nwhile (x) {\nx--;\n}\n}\n}\n"
        context = control_context(source, 8)
        self.assertIn("if branch start line 3; branch end line 10", context)
        self.assertIn("if branch start line 5; branch end line 10", context)
        self.assertIn("while loop start line 7; loop end line 9", context)
        self.assertEqual(control_context(source, 11), "")
        # The original raw-token walk steps beyond its last token on this input.
        guarded = "#if PAL\nif (x) if (y) {\n#else\nif (x) if (y) {\n#endif\nx--;\n}\n"
        self.assertIn("branch end line 7", control_context(guarded, 6))
        for source in ("if (x) if (y) {}", "if (x) {", "if (x)\n#if PAL\n{\n#endif\n}"):
            with self.subTest(source=source):
                self.assertIsInstance(control_context(source, 1), str)
