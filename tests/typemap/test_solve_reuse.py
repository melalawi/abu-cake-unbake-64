"""A solve whose inputs match the last published solution's leaves it standing; a forced recompute does not."""

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.kit import BUDGET_HOST, TempCase
from unbake import steps, tui
from unbake.typemap import declarations, solver, types_db
from unbake.typemap import facts as source_facts

SAME = "The type inputs did not change, so the last solution stands"


class SolveReuseTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = SimpleNamespace(
            root=self.root,
            build=self.root / "build",
            include=(self.root / "include",),
            cppflags=(),
            id="p",
            versions=("us",),
            version=lambda version: SimpleNamespace(baserom_sha1="x"),
        )
        (self.root / "build/map").mkdir(parents=True)
        for name in ("build/map/facts.json", "config.toml", "layout.toml"):
            (self.root / name).write_text(name)
        self.database = self.root / "build/types.sqlite"
        self.published = MagicMock(side_effect=lambda *args, **named: self.database.write_text("solution"))
        self.infer = MagicMock(return_value={})
        self.seeds: list[dict] = [{"functions": {}, "aliases": {"s32": "int"}}]
        self.missing: list[str] = []

    def solve(self, publish: MagicMock | None = None) -> dict:
        facts = {"shard_sha256": "s", "shard": {}, "abi_supplement": None}
        refine = patch("unbake.typemap.abi_facts.refine", return_value=facts)
        with (
            refine,
            patch.object(solver, "refresh_map", return_value={}),
            patch.object(source_facts, "published_keys", return_value=[]),
            patch.object(declarations, "collect", side_effect=lambda *args: self.seeds),
            patch.object(solver, "_evidence", return_value=({}, {}, {})),
            patch.object(solver, "infer", self.infer),
            patch.object(types_db, "path", return_value=self.database),
            patch.object(types_db, "summary", return_value={}),
            patch("unbake.typemap.database.publish", publish or self.published),
            patch("unbake.layout.header_step.missing", side_effect=lambda project: self.missing),
        ):
            return solver.solve(self.project, None)

    def marker(self) -> Path:
        return solver.marker(self.project)

    def test_same_inputs_twice_reuse_the_solution(self) -> None:
        err = io.StringIO()
        self.solve()
        with patch("sys.stderr", err):
            again = self.solve()
        self.assertEqual(again, {"changes": {}, "reused": True})
        self.assertEqual((self.infer.call_count, self.published.call_count), (1, 1))
        self.assertIn(SAME, err.getvalue())
        tui.stop()

    def test_one_seed_differs_so_the_solve_runs_and_the_marker_moves(self) -> None:
        self.solve()
        first = self.marker().read_text()
        self.seeds = [{"functions": {}, "aliases": {"s32": "long"}}]
        result = self.solve()
        self.assertNotIn("reused", result)
        self.assertEqual((self.infer.call_count, self.published.call_count), (2, 2))
        self.assertNotEqual(self.marker().read_text(), first)

    def test_a_missing_database_or_header_solves_again(self) -> None:
        for label in ("types database missing", "generated header missing"):
            with self.subTest(label):
                self.solve()
                before = self.infer.call_count
                if label.startswith("types"):
                    self.database.unlink()
                else:
                    self.missing = ["common/x.h"]
                self.solve()
                self.assertEqual(self.infer.call_count, before + 1)
                self.missing = []

    def test_a_failed_publish_leaves_no_marker(self) -> None:
        self.solve()
        self.assertTrue(self.marker().is_file())
        self.seeds = [{"functions": {}, "aliases": {}}]
        with self.assertRaises(RuntimeError):
            self.solve(MagicMock(side_effect=RuntimeError("no")))
        self.assertFalse(self.marker().exists())

    def test_a_forced_recompute_of_types_removes_the_marker_before_ensure(self) -> None:
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text("key")
        seen = []
        with patch.object(
            steps, "ensure", side_effect=lambda *args, **named: seen.append(self.marker().exists()) or []
        ):
            steps.recompute(self.project, BUDGET_HOST, ["types"])
            self.assertEqual(seen, [False])
            self.marker().write_text("key")
            steps.recompute(self.project, BUDGET_HOST, ["buildfiles"])
        self.assertEqual(seen, [False, True])  # a recompute that never runs types keeps it


if __name__ == "__main__":
    import unittest

    unittest.main()
