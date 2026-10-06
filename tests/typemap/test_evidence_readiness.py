"""Repeated declaration collection must not invalidate an unchanged public solve."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import inputs
from unbake.typemap import declarations, facts, solver


class EvidenceReadinessTests(ProjectCase):
    def test_unchanged_collected_evidence_reuses_readiness_but_changed_input_does_not(self):
        header = self.project.include[0] / "evidence.h"
        header.write_text("/* unbake declaration evidence: evidence_1234abcd */\nextern int cell;\n")
        (self.project.build / "map").mkdir()
        (self.project.build / "map/facts.json").write_text("{}")
        mapped = {"shard_sha256": "fixture", "shard": {}, "abi_supplement": None}
        with (
            patch("unbake.layout.index.headers", return_value=[header]),
            patch.object(solver, "refresh_map", return_value=mapped),
            patch("unbake.typemap.abi_facts.refine", return_value=mapped),
            patch.object(facts, "published_keys", wraps=facts.published_keys) as keys,
        ):
            before = declarations.collect(self.project, None, [])
            first = solver.readiness(self.project, None)
            signature = inputs.signature(self.project.build / "types/declaration_evidence.c")
            after = declarations.collect(self.project, None, [])
            self.assertEqual(after, before)
            self.assertEqual(solver.readiness(self.project, None).key, first.key)
            self.assertEqual(keys.call_count, 1)
            self.assertEqual(inputs.signature(self.project.build / "types/declaration_evidence.c"), signature)
            header.write_text("/* unbake declaration evidence: evidence_1234abcd */\nextern float cell;\n")
            self.assertNotEqual(solver.readiness(self.project, None).key, first.key)
            self.assertEqual(keys.call_count, 2)
            self.assertNotEqual(declarations.collect(self.project, None, []), before)
