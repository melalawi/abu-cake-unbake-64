"""A failed cold object cannot prevent later objects from becoming reusable."""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.project_tools import compile as compiler


class CompileChunkTests(unittest.TestCase):
    def test_compile_failure_keeps_objects_after_both_bad_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "src"
            source.mkdir()
            units = [source / (name + ".c") for name in ("good_a", "bad_a", "good_b", "bad_b", "good_c")]
            args = argparse.Namespace(
                source=source, output=root / "obj", kind="cc", batch=units, recipe=root / "recipe"
            )
            visited = []

            def build(item, data):
                visited.append(item.source.stem)
                if item.source.stem.startswith("bad"):
                    raise ValueError("syntax error")
                item.output.parent.mkdir(parents=True, exist_ok=True)
                item.output.write_bytes(b"retained object")

            with (
                patch.object(compiler, "read_recipe", return_value={}),
                patch.object(compiler, "compile_object", build),
                self.assertRaisesRegex(ValueError, r"(?s)bad_a.c: syntax error.*bad_b.c: syntax error"),
            ):
                compiler.compile_batch(args)
            self.assertEqual(visited, [unit.stem for unit in units])
            for name in ("good_a", "good_b", "good_c"):
                self.assertEqual((root / "obj" / (name + ".o")).read_bytes(), b"retained object")
                self.assertTrue((root / "obj" / (name + ".built")).is_file())

    def test_proof_batch_stops_at_first_diagnostic_and_cancels_other_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "src"
            source.mkdir()
            units = [source / (name + ".c") for name in ("bad", "queued")]
            args = argparse.Namespace(
                source=source,
                output=root / "obj",
                kind="cc",
                batch=units,
                recipe=root / "recipe",
                cancel_file=root / "cancel",
                version="us",
            )
            visited = []

            def build(item, data):
                visited.append((item.version, item.source))
                raise ValueError("duplicate typedef")

            with (
                patch.object(compiler, "read_recipe", return_value={}),
                patch.object(compiler, "compile_object", side_effect=build),
            ):
                with self.assertRaisesRegex(ValueError, "bad.c: duplicate typedef"):
                    compiler.compile_batch(args)
                self.assertTrue(args.cancel_file.exists())
                args.version = "eu"
                compiler.compile_batch(args)
            self.assertEqual(visited, [("us", units[0])])
            self.assertFalse(list(root.rglob("*.built")))

    def test_assembly_batch_selects_nested_symbol_inputs_and_retains_legacy_file(self) -> None:
        for scoped in (False, True):
            with self.subTest(scoped=scoped), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                symbols = root / "symbols"
                if scoped:
                    symbols.mkdir()
                else:
                    symbols.write_text("global 0x80000000\n")
                sources = [root / "asm" / path for path in ("first.s", "nested/second.s")]
                args = argparse.Namespace(
                    source=root / "asm",
                    output=root / "obj",
                    kind="as",
                    batch=sources,
                    recipe=root / "recipe",
                    symbols=symbols,
                )
                visited = []

                def build(item, data, visited=visited):
                    visited.append((item.source, item.symbols, item.dep_target))
                    item.output.parent.mkdir(parents=True, exist_ok=True)

                with (
                    patch.object(compiler, "read_recipe", return_value={}),
                    patch.object(compiler, "compile_object", side_effect=build),
                ):
                    compiler.compile_batch(args)
                self.assertEqual(
                    visited,
                    [
                        (
                            source,
                            symbols / source.relative_to(args.source).with_suffix(".txt") if scoped else symbols,
                            "$(BUILD)/obj/asm/" + str(source.relative_to(args.source).with_suffix(".built")),
                        )
                        for source in sources
                    ],
                )
