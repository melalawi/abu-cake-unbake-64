"""The per-unit link: missing address-named symbols resolve at their address; anything else is a refusal."""

import shutil
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles, runner
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


class OtherVersionNameTests(unittest.TestCase):
    """A name another VERSION's address gave an object resolves through the shared-name anchors."""

    def project(self, tables: dict[str, dict[str, int]]) -> SimpleNamespace:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        versions = {}
        for version, table in tables.items():
            path = root / f"{version}.txt"
            path.write_text("".join(f"{name} = 0x{address:08X};\n" for name, address in table.items()))
            versions[version] = SimpleNamespace(symbols=path)
        return SimpleNamespace(names_from="eu", versions=tuple(tables), version=versions.__getitem__)

    def test_name_resolves_at_the_same_object_in_the_other_version(self) -> None:
        project = self.project(
            {
                "eu": {"gA": 0x100, "gB": 0x200, "D_80000150_eu": 0x150},
                "de": {"gA": 0x110, "gB": 0x210},
            }
        )
        self.assertEqual(
            runner.derived_symbols({"D_80000150_eu"}, frozenset(), "de", SOURCE, project),
            ["--defsym=D_80000150_eu=0x00000160"],
        )

    def test_a_listed_name_is_kept_and_unanchored_or_unknown_names_are_refused_with_the_version(self) -> None:
        project = self.project(
            {
                "eu": {"gA": 0x100, "gB": 0x200, "D_80000150_eu": 0x150},
                "de": {"gA": 0x110, "gB": 0x230, "D_80000150_eu": 0x999},
            }
        )
        self.assertEqual(
            runner.derived_symbols({"D_80000150_eu"}, frozenset({"D_80000150_eu"}), "de", SOURCE, project), []
        )
        project = self.project(
            {
                "eu": {"gA": 0x100, "gB": 0x200, "gC": 0x300, "D_80000150_eu": 0x150},
                "de": {"gA": 0x110, "gB": 0x230, "gC": 0x330},
            }
        )
        for label, name in [("anchors disagree", "D_80000150_eu"), ("listed nowhere", "gNowhere")]:
            with self.subTest(label), self.assertRaises(Held) as caught:
                runner.derived_symbols({name}, frozenset(), "de", SOURCE, project)
            self.assertIn(f"VERSION de: {name}", caught.exception.reason)


class SymbolsLdOtherVersionTests(OtherVersionNameTests):
    """symbols.ld provides the same address for a version-suffixed name as the probe link does."""

    def render(self, project: SimpleNamespace, version: str, suffixed: dict[str, str]) -> str:
        with patch("unbake.compilers.runtime.bindings", return_value={}):
            return buildfiles.symbols_ld(project, version, {}, suffixed)  # type: ignore[arg-type]

    def test_the_final_link_agrees_with_the_probe_link(self) -> None:
        project = self.project(
            {
                "eu": {"gA": 0x100, "gB": 0x200, "D_80000150_eu": 0x150},
                "de": {"gA": 0x110, "gB": 0x210},
            }
        )
        text = self.render(project, "de", {"D_80000150_eu": "src/a.c"})
        self.assertIn("PROVIDE(D_80000150_eu = 0x00000160);\n", text)
        self.assertEqual(
            runner.derived_symbols({"D_80000150_eu"}, frozenset(), "de", SOURCE, project),  # type: ignore[arg-type]
            ["--defsym=D_80000150_eu=0x00000160"],
        )
        self.assertIn("PROVIDE(D_80000150_eu = 0x00000150);\n", self.render(project, "eu", {"D_80000150_eu": "a.c"}))

    def test_unanchored_is_refused_naming_source_version_symbol_and_unlisted_names_are_not_references(self) -> None:
        project = self.project(
            {
                "eu": {"gA": 0x100, "gB": 0x200, "gC": 0x300, "D_80000150_eu": 0x150},
                "de": {"gA": 0x110, "gB": 0x230, "gC": 0x330},
            }
        )
        with self.assertRaises(Held) as caught:
            self.render(project, "de", {"D_80000150_eu": "src/a.c"})
        for part in ("src/a.c", "VERSION de", "D_80000150_eu"):
            self.assertIn(part, caught.exception.reason)
        self.assertNotIn("func_80001000_de", self.render(project, "de", {"func_80001000_de": "src/a.c"}))

    def test_the_source_scan_finds_suffixed_names_apart_from_address_names(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        source = root / "a.c"
        source.write_text("u8 *p = &D_80000150_eu; u8 *q = &D_80000150; void func_80001000_us(void);\n")
        named, suffixed = buildfiles._address_names([source])
        self.assertEqual(named, {"D_80000150": 0x80000150})
        self.assertEqual(suffixed, {"D_80000150_eu": str(source), "func_80001000_us": str(source)})


class LinkRefusalTests(ProjectCase):
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
        source = self.project.work / SOURCE.name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("void func_800C3EF0_us(void) {}\n")
        with (
            patch("unbake.work.source_scope.admit_source", return_value=SimpleNamespace(subject="func_800C3EF0_us")),
            patch.object(compare.split, "holding_versions", return_value=("us",)),
            patch.object(compare, "view_for", return_value=self.project),
            patch.object(compare, "row_of", return_value=row),
            patch.object(compare.split, "words", return_value=b"\0" * 16),
            patch.object(runner, "compile_unit", return_value=nullcontext(Path("unit.o"))),
            patch.object(runner, "link_function", side_effect=refusal),
            self.assertRaises(Held) as caught,
        ):
            compare.measure(self.project, self.host, source)
        self.assertIs(caught.exception, refusal)

    def test_an_undefined_symbol_is_not_retried_under_other_compilers(self) -> None:
        refusal = Held(
            named("link.undefined", f"link.undefined: {SOURCE}: VERSION us: gMissing", owner="fixture", stage="link")
        )
        project = SimpleNamespace(compiler_reference=lambda function: "gcc")
        source = Path(tempfile.mkdtemp()) / SOURCE.name
        self.addCleanup(shutil.rmtree, source.parent)
        source.write_text("void func_800C3EF0_us(void) {}\n")
        with (
            patch.object(candidates.choice, "alternatives") as alternatives,
            self.assertRaises(Held) as caught,
        ):
            candidates.resolve(project, SimpleNamespace(), source, refusal)
        self.assertIs(caught.exception, refusal)
        alternatives.assert_not_called()
