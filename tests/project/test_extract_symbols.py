"""Symbol inventory includes labels inside compiled C intervals."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helper_fixture import extraction
from tests.project.makefile_fixture import fixture, helper, write_rendered


class DiscoveredSymbols(unittest.TestCase):
    def test_canonical_unit_alias_links_and_real_definition_wins(self) -> None:
        self.addCleanup(patch.stopall)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, _ = fixture(root, case=self)
            write_rendered(project)
            symbols = root / "versions/us/symbol_addrs.txt"
            symbols.write_text("")
            splat = root / "tools/splat"
            splat.write_text(splat.read_text() + "dump.write_text('name,vram_start\\nfirst_auto,80000000\\n')\n")
            result = extraction(project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            definitions = root / "build/us/committed_symbols.ld"
            self.assertIn("PROVIDE(first = 0x80000000);", definitions.read_text())
            symbols.write_text("first = 0x80000004;\n")
            result = extraction(project)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("split and symbols disagree for first", result.stderr)

    def test_linker_alias_comments_survive_fresh_extraction(self) -> None:
        self.addCleanup(patch.stopall)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, _ = fixture(root, case=self)
            write_rendered(project)
            symbols = project.version("us").symbols
            symbols.write_text("native = 0x80001000;\n// unbake linker alias: displaced = 0x80001000;\n")
            result = extraction(project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            generation = project.build_link("us")
            bindings = (generation / "committed_symbols.ld").read_text()
            for name in ("native", "displaced"):
                self.assertIn(f"PROVIDE({name} = 0x80001000);", bindings)
                self.assertIn(f"{name} 0x80001000", (generation / "symbol-addresses.txt").read_text())

    def test_c_labels_and_conflicts(self) -> None:
        extract = helper("extract")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "symbols.csv"
            path.write_text("vram_start,name,subsegment_type\n80201004,inner,c\n800C2000,constant,c\n")
            self.assertEqual(
                extract.discovered_symbols(path, {"entry": 0x80201000}),
                {"entry": 0x80201000, "inner": 0x80201004, "constant": 0x800C2000},
            )
            with self.assertRaisesRegex(ValueError, "conflicting discovered symbol"):
                extract.discovered_symbols(path, {"inner": 0x80201008})

    def test_extraction_suppresses_recovered_output_and_preserves_failure(self) -> None:
        self.addCleanup(patch.stopall)
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as directory:
                patch.stopall()
                root = Path(directory).resolve()
                project, _ = fixture(root, case=self)
                write_rendered(project)
                splat = root / "tools/splat"
                splat.write_text(
                    splat.read_text()
                    + "\nimport sys\nprint('recovered symbol diagnostic')\n"
                    + "print('extractor diagnostic', file=sys.stderr)\n"
                    + ("sys.exit(7)\n" if fails else "")
                )
                result = extraction(project)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode != 0, fails, output)
                self.assertEqual("extractor diagnostic" in output, fails)
                self.assertEqual("recovered symbol diagnostic" in output, fails)
                self.assertEqual("HELD(extract)" in output, fails)


class AssemblySymbolInputs(unittest.TestCase):
    def test_scoped_symbols_cover_source_include_closure_and_unit_placement(self) -> None:
        from unbake.project_tools import extract

        cases = (
            (".word target\n", "", {"first", "target"}),
            ('/* target */\n# target\n.ascii "target"\n', "", {"first"}),
            ('#include "macro.inc"\n', ".word target\n", {"first", "target"}),
            ('.include "macro.inc"\n', "#define LOAD target\n", {"first", "target"}),
            ('#include "macro.inc"\n', '#include "macro.inc"\n.word target\n', {"first", "target"}),
            (".word target_extra\n", "", {"first"}),
        )
        for source, header, expected in cases:
            with self.subTest(source=source, header=header), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                assembly, includes = root / "asm", root / "include"
                (assembly / "nested").mkdir(parents=True)
                includes.mkdir()
                (assembly / "nested/first.s").write_text(source)
                (assembly / "other.s").write_text(".word other\n")
                (includes / "macro.inc").write_text(header)
                symbols = {"first": 0x80000000, "target": 0x80001000, "other": 0x80002000}
                outputs = {}
                with patch.object(
                    extract,
                    "publish",
                    side_effect=lambda path, content, outputs=outputs: outputs.update({path: content}),
                ):
                    extract.assembly_symbols(assembly, includes, symbols, {"first": symbols["first"]}, root)
                table = outputs[root / "asm-symbols/nested/first.txt"].decode().splitlines()
                self.assertEqual({line.split()[0] for line in table}, expected)
                self.assertIn("first 0x80000000 unit", table)
                self.assertEqual(outputs[root / "asm-symbols/other.txt"], b"other 0x80002000\n")

    def test_publication_changes_only_referencing_tables(self) -> None:
        import os

        from unbake.project_tools import extract

        for change, expected in (
            ({"target": 0x80001004}, {"first"}),
            ({"unrelated": 0x80002004}, set()),
            ({"first": 0x80000004}, {"first"}),
            ({"new": 0x80003000}, {"second"}),
            ({"target": None}, {"first"}),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                assembly = root / "asm"
                assembly.mkdir()
                (assembly / "first.s").write_text(".word target\n")
                (assembly / "second.s").write_text(".word new\n")
                symbols = {"first": 0x80000000, "target": 0x80001000, "unrelated": 0x80002000}
                extract.assembly_symbols(assembly, root / "includes", symbols, {}, root)
                tables = list((root / "asm-symbols").glob("*.txt"))
                for path in tables:
                    os.utime(path, ns=(100, 100))
                for name, value in change.items():
                    if value is None:
                        del symbols[name]
                    else:
                        symbols[name] = value
                extract.assembly_symbols(assembly, root / "includes", symbols, {}, root)
                self.assertEqual({path.stem for path in tables if path.stat().st_mtime_ns != 100}, expected)

    def test_missing_include_fails_before_publishing_its_table(self) -> None:
        from unbake.project_tools import extract

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.s").write_text('#include "missing.inc"\n')
            with (
                patch.object(extract, "publish") as publish,
                self.assertRaisesRegex(ValueError, "assembly include missing.inc"),
            ):
                extract.assembly_symbols(root, root / "include", {}, {}, root / "build")
            publish.assert_not_called()

    def test_graph_excludes_ranges_and_global_symbols_from_object_inputs(self) -> None:
        from unbake.project import makefile
        from unbake.project_tools import extract

        self.addCleanup(patch.stopall)
        for kind in ("ido", "sn64"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                patch.stopall()
                project, _ = fixture(root, kind, case=self)
                rendered = makefile.render(project)["Makefile"]
                rules = makefile.compile_rules(project)
                self.assertNotIn("unit-ranges", rules)
                self.assertNotIn("symbol-addresses", rules)
                link = next(line for line in rendered.splitlines() if line.startswith("$(ELF):"))
                self.assertIn("$(SYMBOLS)", link)
                self.assertIn("$(LINK_SCRIPTS)", link)
                self.assertIn("unit-ranges", link)
                _, graph = extract.inventory(
                    "SECTIONS { asm/nested/first.s.o(.text); src/middle.c.o(.text); }",
                    root,
                    Path("asm/us"),
                    Path("src"),
                    kind,
                )
                symbol_edges = [line for line in graph if "asm-symbols" in line]
                self.assertEqual(bool(symbol_edges), kind == "sn64")
                if kind == "sn64":
                    self.assertIn("$(BUILD)/obj/asm/nested/first.built: $(BUILD)/asm-symbols/nested/first.txt", graph)
                self.assertFalse(any("unit-ranges" in line or "symbol-addresses" in line for line in graph))
