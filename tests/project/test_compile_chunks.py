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
            root = Path(temporary)
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
