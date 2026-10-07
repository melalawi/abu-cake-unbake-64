"""The solve receipt controls reuse without being mistaken for a semantic input."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import inputs
from unbake.typemap import facts, solver


class ReadinessMarkerTests(ProjectCase):
    def test_publishing_or_removing_receipt_does_not_repeat_source_key_work(self):
        (self.project.build / "map").mkdir()
        (self.project.build / "map/facts.json").write_text("{}")
        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) {return 1;}\n")
        mapped = {"shard_sha256": "fixture", "shard": {}, "abi_supplement": None}
        with (
            patch.object(solver, "refresh_map", return_value=mapped),
            patch("unbake.typemap.abi_facts.refine", return_value=mapped),
            patch.object(
                facts,
                "published_keys",
                side_effect=lambda *args: [inputs.digest(source, algorithm="sha256", reuse=True)],
            ) as keys,
        ):
            first = solver.readiness(self.project, None)
            marker = solver.marker(self.project)
            marker.parent.mkdir(exist_ok=True)
            marker.write_text(first.key)
            self.assertEqual(solver.readiness(self.project, None).key, first.key)
            marker.unlink()
            self.assertEqual(solver.readiness(self.project, None).key, first.key)
            self.assertEqual(keys.call_count, 1)
            source.write_text("int alpha(void) {return 2;}\n")
            self.assertNotEqual(solver.readiness(self.project, None).key, first.key)
            self.assertEqual(keys.call_count, 2)
