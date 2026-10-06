"""Complete current input snapshots skip collect/evidence and advance existing SQLite revisions."""
import io
from unittest.mock import MagicMock, patch
from tests.project_fixture import ProjectCase
from unbake import inputs, tui
from unbake.config import Held
from unbake.typemap import declarations, facts, solver, types_db


class SolveReuseTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        (self.project.build / "map").mkdir(exist_ok=True)
        (self.project.build / "map/facts.json").write_text('{}')
        self.source = self.project.src / 'alpha.c'
        self.source.write_text('int alpha(void) { return 1; }\n')
        self.database = types_db.path(self.project)
        self.infer = MagicMock(return_value={})
        self.collect = MagicMock(return_value=[{"functions": {}, "aliases": {}}])
        self.evidence = MagicMock(return_value=({}, {}, {}))
        self.missing = []
        self.published = MagicMock(side_effect=self.publish)

    def publish(self, project, result, previous, **named):
        staged, _ = types_db.stage(self.database, types_db.encode(result), {}, {})
        types_db.install(self.database, staged)

    def solve(self, publish=None):
        mapped = {"shard_sha256": "s", "shard": {}, "abi_supplement": None}
        with (patch.object(solver, "refresh_map", return_value={}),
              patch("unbake.typemap.abi_facts.refine", return_value=mapped),
              patch.object(facts, "published_keys", side_effect=lambda *args: [inputs.digest(self.source)]),
              patch.object(declarations, "collect", self.collect),
              patch.object(solver, "_evidence", self.evidence),
              patch.object(solver, "infer", self.infer),
              patch("unbake.typemap.database.publish", publish or self.published),
              patch("unbake.layout.header_step.missing", side_effect=lambda project: self.missing)):
            return solver.solve(self.project, None)

    def test_unchanged_snapshot_reuses_before_collect_or_evidence(self):
        self.solve()
        before = self.collect.call_count, self.evidence.call_count
        with patch('sys.stderr', io.StringIO()):
            again = self.solve()
        self.assertEqual(again, {"changes": {}, "reused": True})
        self.assertEqual((self.collect.call_count, self.evidence.call_count), before)
        self.assertEqual(types_db.meta(self.database, 'revision'), 1)
        tui.stop()

    def test_changed_source_advances_revision_and_retains_semantic_digest(self):
        self.solve()
        first_key = solver.marker(self.project).read_text()
        first_digest = types_db.meta(self.database, 'solution_sha256')
        self.source.write_text('int alpha(void) { return 2; }\n')
        self.solve()
        self.assertEqual(types_db.meta(self.database, 'revision'), 2)
        self.assertEqual(types_db.meta(self.database, 'solution_sha256'), first_digest)
        self.assertNotEqual(solver.marker(self.project).read_text(), first_key)
        self.assertEqual(self.published.call_count, 2)

    def test_missing_header_and_forced_marker_require_a_solve(self):
        self.solve()
        self.missing = ['common/x.h']
        self.solve()
        self.assertEqual(types_db.meta(self.database, 'revision'), 2)
        self.missing = []
        solver.marker(self.project).unlink()
        self.solve()
        self.assertEqual(types_db.meta(self.database, 'revision'), 3)

    def test_invalid_existing_revision_is_named_and_publish_failure_removes_marker(self):
        self.solve()
        with patch.object(types_db, 'meta', return_value=True):
            with self.assertRaisesRegex(Held, 'meta.revision'):
                self.solve()
        self.source.write_text('int alpha(void) { return 3; }\n')
        with self.assertRaises(RuntimeError):
            self.solve(MagicMock(side_effect=RuntimeError('install failed')))
        self.assertFalse(solver.marker(self.project).exists())
