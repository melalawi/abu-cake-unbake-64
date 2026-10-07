"""The per-unit link: missing address-named symbols resolve at their address; anything else is a refusal."""

import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake import runner
from unbake.compilers import candidates
from unbake.config import Held
from unbake.process import named
from unbake.work import compare

SOURCE = Path("build/work/func_800C3EF0_us/func_800C3EF0_us.c")


class DerivedSymbolsTests(unittest.TestCase):
    def test_each_missing_symbol(self) -> None:
        known = frozenset({"D_80000010", "gKnown"})
        for label, names, expected in [
            ("all known", {"D_80000010", "gKnown"}, []),
            ("missing data address", {"D_80327E50"}, ["--defsym=D_80327E50=0x80327E50"]),
            ("missing function address", {"func_80001234", "gKnown"}, ["--defsym=func_80001234=0x80001234"]),
            ("lowercase is not an address name", {"D_80327e50"}, None),
            ("a plain name", {"gMissing", "D_80327E50"}, None),
        ]:
            with self.subTest(label):
                if expected is None:
                    with self.assertRaises(Held) as caught:
                        runner.derived_symbols(names, known, "us", SOURCE)
                    self.assertEqual(caught.exception.key, "link.undefined")
                    self.assertIn(str(SOURCE), caught.exception.reason)
                    self.assertIn(min(names - {"D_80327E50"}), caught.exception.reason)
                else:
                    self.assertEqual(runner.derived_symbols(names, known, "us", SOURCE), expected)


class LinkRefusalTests(unittest.TestCase):
    def test_a_link_refusal_is_never_a_zero_percent_compare(self) -> None:
        refusal = Held(
            named(
                "link.undefined",
                f"link.undefined: {SOURCE}: VERSION us: gMissing in neither ...",
                owner="fixture",
                stage="link",
            )
        )
        row = SimpleNamespace()
        with (
            patch.object(compare, "function_of", return_value="func_800C3EF0_us"),
            patch.object(compare.split, "holding_versions", return_value=("us",)),
            patch.object(compare, "view_for", return_value=SimpleNamespace()),
            patch.object(compare, "row_of", return_value=row),
            patch.object(compare.split, "words", return_value=b"\0" * 16),
            patch.object(runner, "compile_unit", return_value=nullcontext(Path("unit.o"))),
            patch.object(runner, "link_function", side_effect=refusal),
            patch.object(Path, "read_bytes", return_value=b"void f(void) {}\n"),
            self.assertRaises(Held) as caught,
        ):
            compare.measure(SimpleNamespace(), SimpleNamespace(), SOURCE)
        self.assertIs(caught.exception, refusal)

    def test_an_undefined_symbol_is_not_retried_under_other_compilers(self) -> None:
        refusal = Held(
            named("link.undefined", f"link.undefined: {SOURCE}: VERSION us: gMissing", owner="fixture", stage="link")
        )
        project = SimpleNamespace(compiler_reference=lambda function: "gcc")
        with (
            patch.object(candidates.choice, "alternatives") as alternatives,
            self.assertRaises(Held) as caught,
        ):
            candidates.resolve(project, SimpleNamespace(), SOURCE, refusal)
        self.assertIs(caught.exception, refusal)
        alternatives.assert_not_called()
