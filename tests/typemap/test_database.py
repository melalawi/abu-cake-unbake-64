"""Content identity, proven feedback and transactional generated type context."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.project.config import Held
from unbake.typemap import clear_redraft, context, feedback, load, map_program, redrafts, solve, storage


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.project, self.policy, _ = fixture(Path(directory.name), versions=("us", "eu"))

    def test_map_keeps_all_versions_and_visible_unknown_types(self) -> None:
        mapped = map_program(self.project)
        self.assertEqual(set(mapped["functions"]["alpha"]["versions"]), {"us", "eu"})
        self.assertEqual(len(mapped["functions"]), 3)
        database = solve(self.project)
        self.assertIsNotNone(load(self.project))
        self.assertEqual(database["functions"]["alpha"]["state"], "unknown")
        self.assertIn("unknown: function:alpha", context(self.project))
        self.assertNotIn("typedef", (self.project.include[0] / "shared/typemap.h").read_text())

    def test_changed_header_and_map_refuse_stale_database(self) -> None:
        map_program(self.project)
        solve(self.project)
        (self.project.include[0] / "types.h").write_text("typedef float s32;\n")
        with self.assertRaisesRegex(Held, "types.inputs_stale"):
            load(self.project)
        self.project.version("us").symbols.write_text("alpha = 0x80001000;\n")
        with self.assertRaisesRegex(Held, "map.inputs_stale"):
            solve(self.project)

    def test_rom_digest_and_missing_symbol_input_are_named(self) -> None:
        self.project.version("eu").baserom.write_bytes(b"changed")
        with self.assertRaisesRegex(Held, "map.rom_sha1.eu"):
            map_program(self.project)
        self.project.version("eu").symbols.unlink()
        with self.assertRaisesRegex(Held, "map.symbols.eu"):
            map_program(self.project)

    def test_solve_marks_changed_function_and_digest_clear_rejects_race(self) -> None:
        map_program(self.project)
        solve(self.project)
        (self.project.include[0] / "signatures.h").write_text("int alpha(void);\n")
        solve(self.project)
        marks = redrafts(self.project)
        self.assertIn("alpha", marks)
        digest = storage.digest((self.project.build / "types/database.json").read_bytes())
        with self.assertRaisesRegex(Held, "types.redraft"):
            clear_redraft(self.project, "alpha", "0" * 64)
        clear_redraft(self.project, "alpha", digest)
        self.assertNotIn("alpha", redrafts(self.project))

    def test_generated_header_failure_rolls_back_every_file(self) -> None:
        map_program(self.project)
        solve(self.project)
        paths = [
            self.project.build / "types/database.json",
            self.project.include[0] / "shared/typemap.h",
            self.project.include[0] / "shared/prototypes.h",
            self.project.build / "types/redraft.json",
        ]
        before = {path: path.read_bytes() for path in paths}
        write = storage.write

        def fail(path: Path, content: bytes) -> None:
            if path == paths[0] and content != before[path]:
                raise OSError("injected database failure")
            write(path, content)

        with patch("unbake.typemap.storage.write", side_effect=fail), self.assertRaisesRegex(OSError, "injected"):
            solve(self.project)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_feedback_refuses_fuzzy_and_unpublished_sources(self) -> None:
        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) { return 1; }\n")
        with self.assertRaisesRegex(Held, "types.feedback.matched"):
            feedback(self.project, "alpha", source, versions=["us", "eu"], proof={})
        mapped = map_program(self.project)
        proof = {
            "matched": True,
            "source_sha256": storage.digest(source.read_bytes()),
            "versions": ["us", "eu"],
            "target_sha256": {v: row["target_sha256"] for v, row in mapped["functions"]["alpha"]["versions"].items()},
        }
        with self.assertRaisesRegex(Held, "types.feedback.matched"):
            feedback(self.project, "alpha", source, versions=["us", "eu"], proof=proof)
        self.assertFalse((self.project.build / "types/proven.json").exists())

    def test_proven_feedback_resolves_signature_and_marks_calling_neighbour(self) -> None:
        # Alpha's delay slot leaves its incoming argument available to beta.
        directory = Path(self.project.root).parent / "other"
        directory.mkdir()
        project, policy, _ = fixture(directory, words=[0x0C000404, 0, 0x03E00008, 0])
        map_program(project)
        solve(project)
        source = project.src / "beta.c"
        source.write_text("int beta(int value) { return value; }\n")
        split = project.version("us").split
        split.write_text(split.read_text().replace(", asm, beta]", ", c, beta]"))
        mapped = map_program(project)
        proof = {
            "matched": True,
            "source_sha256": storage.digest(source.read_bytes()),
            "versions": ["us"],
            "target_sha256": {"us": mapped["functions"]["beta"]["versions"]["us"]["target_sha256"]},
        }
        result = feedback(project, "beta", source, versions=["us"], proof=proof, policy=policy)
        self.assertEqual(result["functions"]["beta"]["state"], "known")
        self.assertIn("alpha", redrafts(project))
        self.assertEqual(result["functions"]["beta"]["provenance"][0]["kind"], "proven")


if __name__ == "__main__":
    unittest.main()
