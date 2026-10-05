"""extract is imported by the type map, so it never imports the type package at load (a worker that starts on it
would crash on the cycle)."""

import ast
import unittest
from pathlib import Path

import unbake.extract


class ImportOrderTests(unittest.TestCase):
    def test_extract_imports_no_type_module_at_the_top_level(self) -> None:
        tree = ast.parse(Path(unbake.extract.__file__).read_text())
        top = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("unbake.typemap")
        ]
        self.assertEqual(top, [])


if __name__ == "__main__":
    unittest.main()
