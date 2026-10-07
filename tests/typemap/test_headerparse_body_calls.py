"""Rendering must distinguish the real version-selected call from a definition."""

import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.layout import redeclarations
from unbake.typemap import database, declarations

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_headerparse"
SOURCE = (FIXTURE / "func_802612A8_de.c").read_text()
HEADER = (FIXTURE / "types.h").read_text() + (FIXTURE / "generated.h").read_text()
OWNER = "func_802612A8_de"
CALLEE = "func_8025F0D4_de"


class HeaderParseBodyCallsTests(unittest.TestCase):
    def shared(self):
        facts = declarations.extract(HEADER, {})
        return database._Drops(
            {name: row["prototype"] for name, row in facts["functions"].items()},
            {},
            {},
            {},
            set(),
            facts["aliases"],
            {},
        )

    def drops(self, shared, text=SOURCE):
        return database._source_drops(shared, (Path(OWNER + ".c"), text))

    def test_real_body_call_never_compares_or_drops_the_callee_contract(self):
        shared = self.shared()
        before = dict(shared.declarations_by_name)
        with patch.object(redeclarations, "equivalent", wraps=redeclarations.equivalent) as compare:
            self.assertEqual(self.drops(shared), (set(), set(), set()))
        self.assertEqual(compare.call_count, 1)
        self.assertIn(OWNER, compare.call_args.args[0])
        self.assertEqual(shared.declarations_by_name, before)

    def test_conflicting_retained_callee_is_not_owned_by_a_body_call(self):
        shared = self.shared()
        path = Path(".published-callee.h")
        shared.components[path] = f"extern void {CALLEE}(void);"
        shared.contracts_by_name[CALLEE] = [path]
        shared.retained_contracts[path] = {CALLEE}
        shared.published_homes.add(path)
        with patch.object(redeclarations, "equivalent", wraps=redeclarations.equivalent) as compare:
            self.assertEqual(self.drops(shared), (set(), set(), set()))
        self.assertEqual(compare.call_count, 1)

    def test_real_definition_conflicts_still_remove_generated_and_retained_contracts(self):
        shared = self.shared()
        path = Path(".published-owner.h")
        shared.components[path] = shared.declarations_by_name[OWNER]
        shared.contracts_by_name[OWNER] = [path]
        shared.retained_contracts[path] = {OWNER}
        shared.published_homes.add(path)
        source = SOURCE.replace("s32 " + OWNER, "void " + OWNER)
        with patch.object(redeclarations, "equivalent", wraps=redeclarations.equivalent) as compare:
            self.assertEqual(self.drops(shared, source), (set(), {OWNER}, {path}))
        self.assertEqual(compare.call_count, 2)

    def test_names_are_generic_and_later_file_scope_definitions_are_checked(self):
        shared = self.shared()
        text = SOURCE
        for name in list(shared.declarations_by_name):
            if name == OWNER:
                continue
            other = "dispatch_" + name.rsplit("_", 1)[-1]
            text = text.replace(name, other)
            shared.declarations_by_name[other] = shared.declarations_by_name.pop(name).replace(name, other)
        text += "\ns32 helper_after(void) { return 1; }\n"
        shared.declarations_by_name["helper_after"] = "extern void helper_after(void);"
        with patch.object(redeclarations, "equivalent", wraps=redeclarations.equivalent) as compare:
            self.assertEqual(self.drops(shared, text), (set(), {"helper_after"}, set()))
        self.assertEqual(compare.call_count, 2)

    def test_repeated_source_scans_bodies_once_and_changed_source_scans_again(self):
        shared = self.shared()
        text = "\n\n\n" + SOURCE
        with patch.object(declarations, "_unit_bodies_blanked", wraps=declarations._unit_bodies_blanked) as scan:
            self.assertEqual(self.drops(shared, text), (set(), set(), set()))
            self.assertEqual(self.drops(shared, text), (set(), set(), set()))
            self.assertEqual(scan.call_count, 1)
            changed = text.replace("s32 " + OWNER, "void " + OWNER)
            self.assertEqual(self.drops(shared, changed), (set(), {OWNER}, set()))
            self.assertEqual(scan.call_count, 2)
