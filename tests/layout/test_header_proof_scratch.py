"""BattleTanx header/draft payloads do not make proof scratch depend on TMPDIR."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.preprocessor import output
from tests.project_fixture import ProjectCase
from unbake.cdecl import parse
from unbake.layout import structs_fold
from unbake.layout.split import Edit


class HeaderProofScratchTests(ProjectCase):
    versions = ("us",)

    def test_evidence_header_and_draft_prove_with_tmpdir_inside_project(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/battletanx_header_proof"
        include = self.project.include[0]
        for path in fixture.rglob("*.h"):
            destination = include / path.relative_to(fixture)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
        draft = (fixture / "func_800942A8_us.c").read_text()
        source = self.project.src / "alpha.c"
        source.write_text('#include "span_1000/code_80091A60.h"\n' + draft)
        header = include / "span_1000/code_80091A60.h"
        before = header.read_text()
        edit = Edit(header, before, before.replace("int tail[2]", "int padding[2]"), ("us",))
        seen = []

        def native(argv, **options):
            root = Path(options["cwd"]).resolve()
            self.assertFalse(root.is_relative_to(self.project.root.resolve()))
            self.assertTrue(root.is_relative_to(self.host.cache_machine_root.resolve()))
            for variable in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(options["env"][variable], str(root))
            self.assertEqual(header.read_text(), before)
            self.assertEqual((root / "include/span_1000/code_80091A60.h").read_text(), edit.after)
            seen.append(root)
            if "-E" in argv:
                expanded = output(argv, **options)
                parsed = parse(expanded.stdout)
                self.assertTrue(parsed.ext)
                self.assertIn("int padding[2]", expanded.stdout)
                self.assertIn("func_800942A8_us", expanded.stdout)
                return expanded
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.dict(os.environ, {"TMPDIR": str(self.project.build)}),
            patch("unbake.compilers.registry.verify"),
            patch("unbake.process.subprocess.run", side_effect=native),
        ):
            structs_fold._compile_includers(self.project, [edit], self.host)
        self.assertGreaterEqual(len(seen), 4)
        self.assertTrue(all(not root.exists() for root in seen))
        self.assertEqual(header.read_text(), before)
