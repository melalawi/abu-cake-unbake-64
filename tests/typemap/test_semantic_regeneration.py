"""Per-source ownership projections reuse exact output across irrelevant lexical/policy changes."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from tests.project_fixture import ProjectCase, make
from unbake import effort
from unbake.config import Host
from unbake.typemap import database, regeneration


class SemanticRegenerationTests(ProjectCase):
    versions = ("us",)

    def test_local_rename_reuses_render_but_declaration_or_dependency_change_invalidates(self):
        source = self.project.src / "alpha.c"
        source.write_text('int alpha(void) { int temp_v0 = 1; return temp_v0; }\n')
        before = regeneration.Session(self.project, self.host)
        source.write_text(source.read_text().replace("temp_v0", "renamed_local"))
        after = regeneration.Session(self.project, self.host)
        self.assertEqual(before.inputs, after.inputs)
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["functions"] = {"alpha": {"state": "known", "prototype": "int alpha(void);"}}
        output = self.project.include[0] / "alpha.h"

        def compute():
            value.update(declaration_headers={"alpha": "alpha.h"}, shared_aliases={})
            return {output: b"int alpha(void);\n"}

        first = before.render(value, compute)
        with patch.object(regeneration.layout_index, "load", return_value={"headers": {}}):
            second = after.render(value, lambda: self.fail("lexical edit rerendered identical headers"))
        self.assertEqual(first, second)
        source.write_text('int alpha(int value) { return value; }\n')
        changed = regeneration.Session(self.project, self.host)
        self.assertNotEqual(changed.inputs, before.inputs)

    def test_environment_omits_cache_resources_and_budgets_but_pins_tools(self):
        old = regeneration.environment(self.project, self.host)
        values = {section: dict(row) for section, row in self.host.values.items()}
        values["cache"]["machine_root"] = str(self.root / "other-cache")
        values["resources"]["memory_worker_bytes"] += 1
        values["budgets"]["changed_seconds"] += 1
        changed = Host.from_values(values, "compare")
        self.assertEqual(regeneration.environment(self.project, changed), old)
        Path(changed.cpp).write_text("changed preprocessor bytes")
        self.assertNotEqual(regeneration.environment(self.project, changed), old)

    def test_validation_bundle_hit_reports_zero_pending_and_the_real_total(self):
        output = self.project.include[0] / "alpha.h"
        session = regeneration.Session(self.project, None)
        with patch.object(database, "_validate_version", return_value="parsed"):
            database.validate_headers(self.project, {output: b"int alpha(void);\n"}, None, abi_context="", session=session)
        before = effort.counted().get("validation.rows", (0, 0))
        database.validate_headers(self.project, {output: b"int alpha(void);\n"}, None, abi_context="", session=session)
        after = effort.counted()["validation.rows"]
        self.assertEqual(after[0] - before[0], 0)
        self.assertGreater(after[1] - before[1], 0)

    def test_owned_conditional_types_follow_effective_unit_macros_without_importing_header_names(self):
        from tests.preprocessor import output
        from unbake import process
        from unbake.typemap import header_names
        source = self.project.src / 'alpha.c'
        source.write_text('#include "types.h"\n#ifdef PRIVATE\ntypedef int Private;\n#endif\nint alpha(void) { return 0; }\n')
        project = replace(self.project, unit_flags={'alpha': ('-DPRIVATE=1',)})
        with patch.object(process.subprocess, 'run', side_effect=output):
            names, tags = header_names._owned((project, self.host, source, source.read_text()))
        self.assertIn('Private', names)
        self.assertNotIn('s32', names)
