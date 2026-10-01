"""Warm match builds use ordinary chunks and do not retry singleton failures."""

from pathlib import Path

from tests.match.support import MatchFixture
from unbake.match import queue, staging


class PublicationSpeedTests(MatchFixture):
    def test_single_failed_candidate_is_built_once_and_remains_queued(self) -> None:
        self.queue("alpha")
        self.build_failures.add(("alpha", "us"))
        receipts = queue.run(self.project, self.policy)
        self.assertEqual(self.calls, [("alpha",)])
        self.assertIn("alpha: build compare failed", receipts[0])
        self.assertEqual([row["function"] for row in self.queued()], ["alpha"])
        self.assert_untouched()

    def test_stale_c_receipts_use_chunks_without_dropping_objects_or_assembly(self) -> None:
        import os

        tools = self.root / "tools"
        driver = tools / "compile.py"
        driver.write_text("driver")
        os.utime(driver, ns=(200, 200))
        generation = self.original["us"]
        for kind, stamp in (("src", 100), ("src", 300), ("asm", 100)):
            with self.subTest(kind=kind, stamp=stamp):
                unit = generation / "obj" / kind / str(stamp)
                unit.parent.mkdir(parents=True, exist_ok=True)
                receipt = unit.with_suffix(".built")
                receipt.touch()
                os.utime(receipt, ns=(stamp, stamp))
                unit.with_suffix(".o").write_bytes(b"warm object")
                unit.with_suffix(".d").write_text("header dependency")
        staging.chunk_stale_sources(generation, tools)
        self.assertFalse((generation / "obj/src/100.built").exists())
        self.assertTrue((generation / "obj/src/300.built").exists())
        self.assertTrue((generation / "obj/asm/100.built").exists())
        for unit in (Path("src/100"), Path("src/300"), Path("asm/100")):
            self.assertEqual((generation / "obj" / unit.with_suffix(".o")).read_bytes(), b"warm object")
            self.assertEqual((generation / "obj" / unit.with_suffix(".d")).read_text(), "header dependency")
