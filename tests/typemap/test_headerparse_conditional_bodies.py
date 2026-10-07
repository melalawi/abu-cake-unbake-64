"""Real version branches share block closings; flattening them invents unclosed bodies."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.typemap.test_headerparse_body_calls import SOURCE as CALL_SOURCE
from unbake import cache, prefixes
from unbake.config import Compiler, Held, Project, Version
from unbake.fold import source_views
from unbake.layout import redeclarations
from unbake.typemap import database, declarations

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_headerparse/func_8026D4F0_de.c"
SOURCE = FIXTURE.read_text()
OWNER = "func_8026D4F0_de"
LATER = "func_8026D834_de"


class HeaderParseConditionalBodiesTests(TempCase):
    def setUp(self):
        super().setUp()
        cache.forget()
        prefixes.forget()
        root = self.root / "project"
        root.mkdir()
        (root / "include").mkdir()
        compiler = Compiler("test", "sn64", Path("cc"), Path("as"), (), Path("sha256"))
        versions = {
            name: Version(name, Path("rom"), "", Path("split"), Path("symbols"), ("VERSION_" + name.upper(),))
            for name in ("de", "eu", "eu_x", "us", "us_rev1")
        }
        self.project = Project(
            root,
            "test",
            "Test",
            "de",
            tuple(versions),
            {"test": compiler},
            "test",
            {},
            versions,
            "test",
            1,
            (),
            ("-E",),
            (),
        )
        self.policy = SimpleNamespace(cpp=Path("/usr/bin/cpp"), cache_machine_root=self.root / "scratch")
        self.path = self.project.src / FIXTURE.name
        prototype = SOURCE.split("{", 1)[0].strip() + ";"
        self.shared = database._Drops(
            {OWNER: prototype},
            {},
            {},
            {},
            set(),
            {"s32": "int", "u8": "unsigned char"},
            {},
            self.project,
            self.policy,
        )

    def test_real_branches_scan_each_version_once_without_parsing_calls(self):
        with (
            patch.object(source_views, "active_source", wraps=source_views.active_source) as select,
            patch.object(declarations, "_unit_bodies_blanked", wraps=declarations._unit_bodies_blanked) as scan,
            patch.object(redeclarations, "equivalent", wraps=redeclarations.equivalent) as compare,
        ):
            self.assertEqual(database._source_drops(self.shared, (self.path, SOURCE)), (set(), set(), set()))
        self.assertEqual(select.call_count, 5)
        self.assertEqual(scan.call_count, 5)
        self.assertEqual(compare.call_count, 1)
        self.assertIn(OWNER, compare.call_args.args[0])
        units = list(declarations.source_definition_units(self.path, SOURCE, self.project, self.policy))
        names = {match["name"] for unit in units for match in database._SIGNATURE.finditer(unit)}
        self.assertEqual(
            names, {OWNER, "func_8026D7D4_de", "func_8026D7F4_de", "func_8026D814_de", LATER, "func_8026D83C_de"}
        )

    def test_later_conflicting_definition_still_drops_generated_and_retained_contracts(self):
        path = self.project.include[0] / "retained.h"
        conflict = f"extern int {LATER}(void);"
        shared = replace(
            self.shared,
            declarations_by_name={**self.shared.declarations_by_name, LATER: conflict},
            components={path: conflict},
            contracts_by_name={LATER: [path]},
            retained_contracts={path: {LATER}},
            published_homes={path},
        )
        with patch.object(redeclarations, "equivalent", wraps=redeclarations.equivalent) as compare:
            self.assertEqual(database._source_drops(shared, (self.path, SOURCE)), (set(), {LATER}, {path}))
        self.assertEqual(compare.call_count, 3)

    def test_malformed_selected_branch_refuses_even_if_flattened_braces_balance(self):
        text = "void broken(void) {\n#if defined(VERSION_DE)\n {\n#else\n }\n#endif\n}\n"
        # This is precisely why a successful raw scan is insufficient.
        declarations._unit_bodies_blanked(declarations.declaration_source(text))
        with self.assertRaises(Held) as raised:
            database._source_drops(self.shared, (self.path, text))
        reason = raised.exception.reason
        for detail in (str(self.path), "de", "broken", "unclosed function body"):
            self.assertIn(detail, reason)

    def test_true_unclosed_real_body_keeps_source_version_and_function_diagnostics(self):
        text = SOURCE.replace("    }\n}\n", "    }\n", 1)
        with self.assertRaises(Held) as raised:
            database._source_drops(self.shared, (self.path, text))
        for detail in (str(self.path), "de", OWNER, "unclosed function body"):
            self.assertIn(detail, raised.exception.reason)

    def test_no_environment_refusal_names_the_source_and_function(self):
        with self.assertRaises(Held) as raised:
            list(declarations.source_definition_units(self.path, SOURCE))
        self.assertIn(str(self.path), raised.exception.reason)
        self.assertIn(OWNER, raised.exception.reason)

    def test_header_macro_is_selected_by_cpp_and_changes_are_not_cached_away(self):
        header = self.project.include[0] / "selection.h"
        text = (
            '#include "selection.h"\n'
            "#if SELECT_VOID\nvoid from_header(void) {\n#else\nint from_header(void) {\n#endif\n}\n"
        )
        shared = replace(self.shared, declarations_by_name={"from_header": "extern void from_header(void);"})
        with (
            patch.object(source_views, "_preprocessed_lines", wraps=source_views._preprocessed_lines) as cpp,
            patch.object(declarations, "_unit_bodies_blanked", wraps=declarations._unit_bodies_blanked) as scan,
        ):
            header.write_text("#define SELECT_VOID 1\n")
            self.assertEqual(database._source_drops(shared, (self.path, text)), (set(), set(), set()))
            self.assertEqual((cpp.call_count, scan.call_count), (5, 1))
            header.write_text("#define SELECT_VOID 0\n")
            self.assertEqual(database._source_drops(shared, (self.path, text)), (set(), {"from_header"}, set()))
            self.assertEqual((cpp.call_count, scan.call_count), (10, 2))

    def test_conditional_calls_without_conditional_braces_keep_the_raw_fast_path(self):
        with patch.object(source_views, "active_source") as select:
            database._source_drops(self.shared, (self.path, CALL_SOURCE))
        select.assert_not_called()
