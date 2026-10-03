"""Regressions for standalone drafts and complete assembly inputs."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.decomp import guide, m2c
from unbake.decomp.draft_context import preprocess_context
from unbake.decomp.draft_input import whole_body
from unbake.project.config import Policy


class DraftInputTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project, self.policy, _ = fixture(self.root, case=self)
        self.policy.state_root = self.root / "state"
        self.policy.m2c = self.root / "m2c"
        self.policy.m2c.write_text('#!/bin/sh\nprintf "typedef int s32;\\nint alpha(void) { return 1; }\\n"\n')
        self.policy.m2c.chmod(0o755)

    def test_duplicate_typedef_dependencies_are_expanded_once(self) -> None:
        (self.project.include[0] / "types.h").write_text("#ifndef TYPES_H\n#define TYPES_H\ntypedef int s32;\n#endif\n")
        for name in ("one", "two"):
            (self.project.include[0] / f"{name}.h").write_text(f'#include "types.h"\nstruct {name} {{ s32 v; }};\n')
        self.policy.m2c.write_text(
            '#!/bin/sh\nprintf "typedef int s32;\\n'
            'int alpha(struct one *a, struct two *b) { return a->v + b->v; }\\n"\n'
        )
        source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.project.work)
        self.assertEqual(
            preprocess_context(source, self.project, cast(Policy, self.policy), "us", "alpha").count(
                "typedef int s32;"
            ),
            1,
        )
        self.assertIn('#include "one.h"', source.read_text())
        self.assertIn('#include "two.h"', source.read_text())

    def test_guide_labels_all_selected_versions(self) -> None:
        project = replace(self.project, versions=("us", "eu"))
        with patch.object(guide, "for_version", return_value="frame: 0x20"), redirect_stdout(io.StringIO()) as output:
            result = guide.run(project, "alpha", None)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(result, "VERSION us\nframe: 0x20\n\nVERSION eu\nframe: 0x20")
        with patch.object(guide, "for_version", return_value="frame: 0x20"):
            self.assertEqual(guide.run(project, "alpha", "us"), "VERSION us\nframe: 0x20")

    def test_draft_announces_function_filename_and_written_path(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            source = m2c.draft(self.project, cast(Policy, self.policy), "alpha", "us", self.project.work)
        self.assertTrue(source.is_file())
        self.assertEqual(source.name, "alpha.c")
        self.assertIn(f"draft_path: {source}", output.getvalue())
        self.assertIn("source filename: alpha.c", output.getvalue())

    def test_internal_function_markers_preserve_the_complete_body(self) -> None:
        assembly = "glabel alpha\nbnez $v0, tail\nendlabel alpha\nglabel tail\njr $ra\nnop\nendlabel tail\n"
        body = whole_body(assembly, "alpha")
        self.assertEqual(body.count("glabel"), 1)
        self.assertIn("bnez $v0, .L_tail", body)
        self.assertIn(".L_tail:\njr $ra\nnop", body)
        self.assertNotIn("endlabel", body)

    def test_private_constants_require_sole_owner_and_exact_rom_bytes(self) -> None:
        import hashlib
        import json
        import struct

        from unbake.decomp.draft_input import private_constants
        from unbake.project.config import Held

        rom = self.project.version("us").baserom
        start = len(rom.read_bytes())
        data = struct.pack(">f", 1.5)
        rom.write_bytes(rom.read_bytes() + data)
        self.project.version("us").symbols.write_text("alpha = 0x80001000;\nliteral = 0x80003000;\n")
        row = {
            "kind": "private",
            "owners": ["alpha"],
            "address": 0x80003000,
            "start": start,
            "end": start + 4,
            "evidence": {
                "kind": "float",
                "safe_sole_candidate": True,
                "writes": [],
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }
        manifest = self.project.root / "build/setup/us.json"
        manifest.parent.mkdir(parents=True)
        body = "glabel alpha\nlui $t0, %hi(literal)\nlwc1 $f0, %lo(literal)($t0)\n"
        manifest.write_text(json.dumps({"providers": [row]}))
        self.assertIn("glabel literal\n.float 1.5", private_constants(self.project, "us", "alpha", body))
        self.project.version("us").symbols.write_text("alpha = 0x80001000;\n")
        (self.project.build_link("us") / "splat_symbols.csv").write_text("name,vram_start\nliteral,80003000\n")
        self.assertIn("glabel literal\n.float 1.5", private_constants(self.project, "us", "alpha", body))
        row["owners"] = ["alpha", "beta"]
        manifest.write_text(json.dumps({"providers": [row]}))
        self.assertEqual(private_constants(self.project, "us", "alpha", body), body)
        row["owners"] = ["alpha"]
        row["evidence"]["writes"] = ["store"]
        manifest.write_text(json.dumps({"providers": [row]}))
        self.assertEqual(private_constants(self.project, "us", "alpha", body), body)
        row["evidence"]["writes"] = []
        manifest.write_text(json.dumps({"providers": [row]}))
        rom.write_bytes(rom.read_bytes()[:-4] + struct.pack(">f", 2.0))
        with self.assertRaisesRegex(Held, "draft.private_constants.*private bytes changed"):
            private_constants(self.project, "us", "alpha", body)

    def test_private_string_bytes_use_existing_symbol_and_escaped_text(self) -> None:
        import hashlib
        import json

        from unbake.decomp.draft_input import private_constants

        rom = self.project.version("us").baserom
        start = len(rom.read_bytes())
        data = b'quote: "hello"\n\0'
        rom.write_bytes(rom.read_bytes() + data)
        self.project.version("us").symbols.write_text("alpha = 0x80001000;\nmessage = 0x80004000;\n")
        row = {
            "kind": "private",
            "owners": ["alpha"],
            "address": 0x80004000,
            "start": start,
            "end": start + len(data),
            "evidence": {
                "kind": "string",
                "safe_sole_candidate": True,
                "writes": [],
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }
        manifest = self.project.root / "build/setup/us.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"providers": [row]}))
        body = "glabel alpha\nlui $a0, %hi(message)\n"
        actual = private_constants(self.project, "us", "alpha", body)
        self.assertIn('glabel message\n.asciz "quote: \\"hello\\"\\n"', actual)
        self.assertEqual(private_constants(self.project, "us", "alpha", "glabel alpha\n"), "glabel alpha\n")
