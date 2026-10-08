"""Content reconstruction of the published C7F50 struct definition identity."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.project import headers
from unbake.project.headers import Graph

FIXTURE = Path(__file__).parent / "fixtures/retained_definition"
PROOF = json.loads((FIXTURE / "provenance.json").read_text())


class RetainedDefinitionTests(ProjectCase):
    versions = ("us-rev1",)

    def setUp(self):
        super().setUp()
        self.project = replace(self.project, id="1317b0d7-6ca5-413c-ae10-0649ad5d6994")
        self.source = self.project.src / PROOF["source"]
        self.source.write_bytes((FIXTURE / PROOF["source"]).read_bytes())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), PROOF["source_sha256"])

    def definitions(self):
        legacy = {}
        sizes = {}
        current = Graph.capture(self.project).initialized_definitions(
            self.project, self.source, "us-rev1", sizes=sizes, legacy_ids=legacy
        )
        return current, legacy, sizes

    def test_actual_struct_reconstructs_retained_and_4b_keys_from_exact_current_content(self):
        current, legacy, sizes = self.definitions()
        self.assertEqual(sizes, {})  # Struct, not an inferred char-array extent.
        self.assertEqual(
            legacy[PROOF["symbol"]],
            (PROOF["definition_proof_id"], "d21c60db2434607f0ab26a97b80c83fbf7b005fc8d3de8f671f7d895db54faa5"),
        )
        graph = Graph.capture(self.project)
        self.assertEqual(graph.closure((self.source,)).paths, ())
        with patch.object(headers, "recipe", return_value="f" * 64):
            self.assertEqual(self.definitions(), (current, legacy, sizes))

    def test_actual_initializer_type_and_header_content_changes_do_not_reuse_retained_key(self):
        raw = self.source.read_bytes()
        for changed in (
            raw.replace(b'Select Team', b'Select None'),
            raw.replace(b'char label', b'short label'),
            b'#include "provider.h"\n' + raw,
        ):
            with self.subTest(content=changed):
                (self.project.include[0] / "provider.h").write_text("typedef int provider;\n")
                self.source.write_bytes(changed)
                _, legacy, _ = self.definitions()
                self.assertNotIn(PROOF["definition_proof_id"], legacy[PROOF["symbol"]])
        before = self.definitions()
        (self.project.include[0] / "provider.h").write_text("typedef short provider;\n")
        self.assertNotEqual(before[:2], self.definitions()[:2])
        self.source.write_bytes(raw)
        self.assertIn(PROOF["definition_proof_id"], self.definitions()[1][PROOF["symbol"]])
