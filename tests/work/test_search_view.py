"""work.search runs on the draft's own include view, as compare does."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake.work import compare, search


class SearchViewTests(TempCase):
    def test_the_search_runs_on_the_drafts_view_not_the_bare_project(self) -> None:
        file = self.root / "work" / "f" / "f.c"
        file.parent.mkdir(parents=True)
        file.write_text("int f(void) { return 0; }\n")
        project = SimpleNamespace(work=self.root / "work")
        view = SimpleNamespace(name="draft view")
        seen: list[object] = []
        outs: list[Path] = []
        best = file.with_name("f.best.c")

        def run(given, host, source, generators, out, seconds):  # type: ignore[no-untyped-def]
            seen.append(given)
            outs.append(out)
            best.write_text("int f(void) { return 0; }\n")
            (out / "steps").write_text("{}\n")
            return SimpleNamespace(
                source=best,
                fuzzy=75.0,
                trial=SimpleNamespace(exact=False),
                steps=out / "steps",
                trials=1,
                score=3,
                skips=({"key": "dumps.greg.unknown_line", "reason": "unsupported row"},),
            )

        with (
            patch.object(
                compare, "view_for", lambda p, f, name: view if (p, Path(f), name) == (project, file, "f") else p
            ),
            patch("unbake.search.core.run", run),
            patch("unbake.search.methods", lambda names: ["generator"]),
        ):
            result = search.search(project, SimpleNamespace(cache_machine_root=self.root / "cache"), file, "order", 5)  # type: ignore[arg-type]
        self.assertEqual(seen, [view])
        self.assertEqual(result.document()["skips"], [{"key": "dumps.greg.unknown_line", "reason": "unsupported row"}])
        self.assertIn("skipped dumps.greg.unknown_line: unsupported row", result.lines())
        # The permuter refuses a work directory inside the project: the search works under the cache.
        self.assertTrue(outs[0].is_relative_to(self.root / "cache"))
        self.assertTrue(outs[0].name.startswith("search-f-"))
        # The scratch is gone when the search ends; the steps log stays beside the best file.
        self.assertFalse(outs[0].exists())
        self.assertTrue(file.with_name("f.steps.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
