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
        best = file.with_name("f.best.c")

        def run(given, host, source, generators, out, seconds):  # type: ignore[no-untyped-def]
            seen.append(given)
            best.write_text("int f(void) { return 0; }\n")
            return SimpleNamespace(
                source=best, fuzzy=75.0, trial=SimpleNamespace(exact=False), steps=out / "steps", trials=1
            )

        with (
            patch.object(
                compare, "view_for", lambda p, f, name: view if (p, Path(f), name) == (project, file, "f") else p
            ),
            patch("unbake.search.core.run", run),
            patch("unbake.search.methods", lambda names: ["generator"]),
        ):
            search.search(project, SimpleNamespace(), file, "order", 5)  # type: ignore[arg-type]
        self.assertEqual(seen, [view])


if __name__ == "__main__":
    unittest.main()
