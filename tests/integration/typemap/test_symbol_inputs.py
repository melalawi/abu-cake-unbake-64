"""Map inputs use the actual extracted symbol-table format and preserve address changes."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import pool
from unbake.typemap import mapping, storage


class ExtractedSymbolTests(ProjectCase):
    versions = ("us",)

    def table(self):
        path = self.project.build_link("us") / "symbol-addresses.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_generated_address_edit_changes_the_map_input_digest(self):
        path = self.table()
        path.write_text("alpha 0x80001000 unit\nextra 0x80003000\n")
        before = storage.map_inputs(self.project)
        path.write_text("alpha 0x80001000 unit\nextra 0x80003004\n")
        self.assertNotEqual(storage.map_inputs(self.project), before)

    def test_extracted_symbols_reach_the_machine_map(self):
        self.table().write_text("alpha 0x80001000 unit\nextra 0x80003000\n")
        with patch.object(
            pool, "run", side_effect=lambda host, fn, items, shared: [fn(shared, item) for item in items]
        ):
            result = mapping.map_program(self.project, self.host)
        self.assertIn("extra", result["symbols"]["us"][0x80003000])
        self.assertEqual(result["globals"]["extra"]["versions"]["us"]["address"], 0x80003000)
