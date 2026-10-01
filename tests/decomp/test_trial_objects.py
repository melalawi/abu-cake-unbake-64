"""Real objdiff rows, symbolic relocations, and build inventory selection."""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.decomp.support import assemble, assembly, fixture
from tests.support import test_policy
from unbake.decomp.score import diff
from unbake.decomp.trial import try_draft
from unbake.decomp.trial_compare import TYPES, compare_object
from unbake.decomp.trial_target import target_object
from unbake.project import build
from unbake.project.config import Held, Policy


class ObjectTrialTests(unittest.TestCase):
    def test_symbol_names_are_differences_without_linking_or_symbol_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = test_policy(root)
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "jal {callee}\nnop\nlui $v0,%hi({data})\nlw $v0,%lo({data})($v0)\njr $ra\nnop\n"
                ".size alpha,.-alpha\n"
            )
            target = assemble(root, "target", body.format(callee="callee", data="global_data"))
            for changed in (False, True):
                with self.subTest(changed=changed):
                    candidate = assemble(
                        root, "draft", body.format(callee="other" if changed else "callee", data="global_data")
                    )
                    document = diff(policy, "us", "alpha", target, candidate, root / "diff.json")
                    result = compare_object("us", document, "alpha")
                    self.assertEqual(set(result.typed), set(TYPES))
                    self.assertEqual(result.typed["relocation"], int(changed))
                    self.assertEqual(result.identical, 5 if changed else 6)
                    if changed:
                        self.assertIn("callee", result.lines[2])
                        self.assertIn("other", result.lines[2])
                    else:
                        self.assertEqual(result.match_percent, 100)

    def test_compile_wrapper_all_versions_and_missing_target_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, policy, source = fixture(root, versions=("us", "eu"))
            content = "#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n"
            source.write_text(content)
            compiled = []

            def compile_source(project: object, policy: object, source: Path, version: str, out: Path) -> Path:
                self.assertTrue(source.read_text().startswith("#define NON_MATCHING 1\n#line 1 "))
                compiled.append(version)
                shutil.copyfile(assemble(out.parent, "compiled", assembly("alpha", [0x24020001, 0x03E00008, 0])), out)
                return out

            with patch.object(build, "compile_object", side_effect=compile_source), redirect_stdout(io.StringIO()):
                result = try_draft(project, cast(Policy, policy), source, root / "scratch")
            self.assertEqual(compiled, ["us", "eu"])
            self.assertTrue(result.identical_everywhere)
            self.assertEqual(source.read_text(), content)
            self.assertFalse(list((root / "scratch").rglob("*.elf")))
            self.assertFalse(list((root / "scratch").rglob("*.bin")))
            generation = build.current_generation(project, "eu")
            (generation / "original.o").unlink()
            with self.assertRaisesRegex(Held, "VERSION eu: target object for alpha is missing"):
                target_object(generation, "alpha", "eu")

    def test_matched_unit_selects_relocatable_input_from_linker_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = assemble(root, "original", assembly("alpha", [0x24020001, 0x03E00008, 0]))
            actual = root / "obj/src/shared.o"
            actual.parent.mkdir(parents=True)
            shutil.copyfile(original, actual)
            (root / "objdiff.json").write_text(
                json.dumps({"units": [{"name": "alpha", "target_path": "wrapper.o", "metadata": {"complete": True}}]})
            )
            (root / "game.ld").write_text("SECTIONS { .text : { obj/src/shared.o(.text) } }\n")
            self.assertEqual(target_object(root, "alpha", "us"), actual.resolve())

    def test_register_and_instruction_edits_feed_allocator_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = test_policy(root)
            target = assemble(root, "target", assembly("alpha", [0x8E020018, 0x03E00008, 0]))
            for words, kind in (
                ([0x8E620018, 0x03E00008, 0], "register"),
                ([0x8E02001C, 0x03E00008, 0], "immediate"),
                ([0x24020001, 0x03E00008, 0], "changed"),
                ([0x8E020018, 0x24030002, 0x03E00008, 0], "inserted"),
            ):
                with self.subTest(kind=kind):
                    candidate = assemble(root, "draft", assembly("alpha", words))
                    result = compare_object(
                        "us", diff(policy, "us", "alpha", target, candidate, root / "diff.json"), "alpha"
                    )
                    self.assertGreater(result.typed[kind], 0)
                    if kind == "register":
                        self.assertEqual(result.register_changes, ((0, 0, 16, 19),))
