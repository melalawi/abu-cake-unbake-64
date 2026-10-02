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

    def test_stale_receipts_use_chunks_without_dropping_objects(self) -> None:
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
        self.assertFalse((generation / "obj/asm/100.built").exists())
        for unit in (Path("src/100"), Path("src/300"), Path("asm/100")):
            self.assertEqual((generation / "obj" / unit.with_suffix(".o")).read_bytes(), b"warm object")
            self.assertEqual((generation / "obj" / unit.with_suffix(".d")).read_text(), "header dependency")

    def test_cli_isolates_one_of_32_failures_in_logarithmic_builds_and_reuses_outputs(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        from unbake.cli.main import main

        names = [f"item_{index:02d}" for index in range(32)]
        for version in self.versions:
            path = self.project.version(version).split
            path.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x1000\n    vram: 0x80001000\n    subsegments:\n"
                + "".join(f"      - [0x{0x1000 + index * 16:X}, asm, {name}]\n" for index, name in enumerate(names))
                + "  - [0x1200]\n"
            )
        self.queue(*names)
        self.build_failures.add((names[17], "eu"))
        reused = []

        def remember(tree: Path, generation_for: object) -> None:
            from collections.abc import Callable
            from typing import cast

            generation = cast(Callable[[str], Path], generation_for)("us")
            marker = generation / "reused-output"
            reused.append(marker.exists())
            marker.write_text("compiled once")

        self.on_build = remember
        output = io.StringIO()
        with (
            patch("unbake.cli.main.config.load", return_value=self.project),
            patch("unbake.cli.main.config.load_policy", return_value=self.policy),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            code = main(
                [
                    "--project",
                    str(self.root),
                    "submit",
                    "--batch",
                    *[str(self.sources / (name + ".c")) for name in names],
                ]
            )
        self.assertEqual(code, 1)
        self.assertLessEqual(len(self.calls), 7)
        self.assertEqual(reused, [False] + [True] * (len(self.calls) - 1))
        self.assertIn(f"HELD(match): {names[17]}:", output.getvalue())
        self.assertEqual({row["function"] for row in self.matched()}, set(names) - {names[17]})
        self.assertEqual([row["function"] for row in self.queued()], [names[17]])
