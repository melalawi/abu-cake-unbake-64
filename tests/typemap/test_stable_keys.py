"""Keys that must survive a moved copy: declared-text facts, header-render environment."""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import pool
from unbake.typemap import declarations, facts, regeneration, storage


class StableKeyTests(TempCase):
    def project(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            root=root, build=root / "build", cache=self.root / "cache", include=(), versions=("us",), id="p"
        )

    def test_text_key_ignores_the_project_root(self) -> None:
        keys: list = []
        captured: list = []
        for name in ("a", "b"):
            root = self.root / name
            project = self.project(root)
            text = f'# 1 "{root}/include/a.h"\nint x;\n'
            header = root / "include" / "a.h"
            captured.clear()
            with patch.object(facts, "text_key", lambda *args: captured.append(args) or "k"):
                store = facts.Store(project, None)
                store.memory["k"] = [{}]
                store.text(text, {"v": 1}, {header}, lambda: {})
            keys.append(captured[0])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0][2], ["include/a.h"])

    def test_collect_extracts_nothing_on_the_second_run(self) -> None:
        project = self.project(self.root / "tree")
        calls: list[str] = []

        def extract(text, provenance, **named):  # type: ignore[no-untyped-def]
            calls.append(text)
            return {"functions": {}, "globals": {}, "structs": {}, "arrays": {}}

        def run_inline(policy, function, jobs):  # type: ignore[no-untyped-def]
            return [function(job) for job in jobs]

        text = f'# 1 "{project.root}/include/a.h"\nint x;\n'
        with (
            patch.object(declarations, "include_headers", lambda *args, **named: []),
            patch.object(declarations, "_version_texts", lambda *args: {"us": text}),
            patch.object(declarations, "extract", extract),
            patch.object(pool, "run", run_inline),
            patch.object(facts, "published", lambda *args: []),
            patch("unbake.typemap.declaration_evidence.feedback_components", lambda project: {}),
        ):
            for run in range(2):
                declarations.collect(project, object(), [])  # type: ignore[arg-type]
                self.assertEqual(len(calls), 1, f"run {run}")

    def test_relocatable_spells_paths_under_the_root_relative(self) -> None:
        root = Path("/work/a")
        value = {
            "root": root,
            "src": root / "src",
            "name": "x",
            "nested": [{"p": str(root / "build" / "us")}, ("/elsewhere/y", root)],
            "count": 3,
        }
        self.assertEqual(
            storage.relocatable(value, root),
            {
                "root": ".",
                "src": "src",
                "name": "x",
                "nested": [{"p": "build/us"}, ["/elsewhere/y", "."]],
                "count": 3,
            },
        )

    def test_environment_is_the_same_at_two_roots(self) -> None:
        from dataclasses import make_dataclass

        keys = []
        for name in ("a", "b"):
            root = self.root / name
            root.mkdir()
            (root / "config.toml").write_text("x")
            shape = make_dataclass("Shape", ["root", "src", "paths"])
            project = shape(root, root / "src", [root / "build"])
            with patch.object(regeneration, "key", lambda *parts: "|".join(str(part) for part in parts[-2:])):
                keys.append(regeneration.environment(project, None))  # type: ignore[arg-type]
            shutil.rmtree(root)
        self.assertEqual(keys[0], keys[1])
