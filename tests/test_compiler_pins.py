"""Compiler pins digest each compiler file once per stat signature, not once per compile."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake import cache, runner


class CompilerPinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "gcc/bin").mkdir(parents=True)
        (self.root / "gcc/bin/cc1").write_bytes(b"one")
        (self.root / "gcc/as").write_bytes(b"two")
        self.project = SimpleNamespace(tools=self.root, compiler_for=lambda unit: SimpleNamespace(id="gcc"))

    def test_unchanged_tree_is_read_once(self) -> None:
        first = runner._compiler_pins(self.project, "u")
        with (
            patch.object(Path, "open", side_effect=AssertionError("re-read")),
            patch.object(cache, "key", wraps=cache.key) as key,
        ):
            self.assertEqual(runner._compiler_pins(self.project, "u"), first)
        self.assertFalse(any(isinstance(part, Path) for call in key.call_args_list for part in call.args))

    def test_changes_change_the_pins(self) -> None:
        first = runner._compiler_pins(self.project, "u")
        for label, change in [
            ("negative: a file's bytes change", lambda: (self.root / "gcc/as").write_bytes(b"three")),
            ("near miss: same bytes, file renamed", lambda: os.rename(self.root / "gcc/as", self.root / "gcc/as2")),
        ]:
            with self.subTest(label):
                change()
                self.assertNotEqual(runner._compiler_pins(self.project, "u"), first)
                first = runner._compiler_pins(self.project, "u")


if __name__ == "__main__":
    unittest.main()
