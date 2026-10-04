"""The headers step: merge-only declarations and include edits only for sources whose needs changed."""

from pathlib import Path
from types import SimpleNamespace

from tests.kit import TempCase
from unbake.layout import headers

# DRAFT interface: solution is {"symbols": {name: group header}}; HeaderPlan(changed, include_edits).


class HeaderPlanTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.include = self.root / "include" / "main"
        self.include.mkdir(parents=True)
        (self.root / "src").mkdir()
        self.project = SimpleNamespace(root=self.root, src=self.root / "src", include=(self.root / "include",))
        self.write_source("a", ["foo"], ["main/g1.h"])
        self.write_source("b", ["bar"], ["main/g2.h"])
        (self.include / "g1.h").write_text("int foo(void);\n")
        (self.include / "g2.h").write_text("int bar(void);\n")

    def write_source(self, name: str, uses: list[str], includes: list[str]) -> None:
        text = "".join(f'#include "{home}"\n' for home in includes)
        text += "".join(f"int {name}_{use}(void) {{ return {use}(); }}\n" for use in uses)
        (self.root / "src" / f"{name}.c").write_text(text)

    def edited_sources(self, plan) -> set[str]:
        return {Path(path).stem for path in plan.include_edits}

    def test_unchanged_solution_edits_nothing(self) -> None:
        plan = headers.plan(self.project, {"symbols": {"foo": "main/g1.h", "bar": "main/g2.h"}})
        self.assertEqual((plan.changed, plan.include_edits), ({}, {}))

    def test_only_sources_whose_needed_names_changed_get_include_edits(self) -> None:
        plan = headers.plan(self.project, {"symbols": {"foo": "main/g1.h", "bar": "main/g3.h"}})
        self.assertEqual(self.edited_sources(plan), {"b"})
        self.assertIn("main/g3.h", next(iter(plan.include_edits.values())))
        self.assertTrue(any(path.name == "g3.h" for path in plan.changed))

    def test_merge_only_never_removes_a_used_declaration(self) -> None:
        plan = headers.plan(self.project, {"symbols": {"bar": "main/g2.h"}})
        for path, data in plan.changed.items():
            if path.name == "g1.h":
                self.assertIn(b"foo", data)
        self.assertNotIn("a", self.edited_sources(plan))

    def test_unused_declaration_may_leave_a_header(self) -> None:
        (self.include / "g2.h").write_text("int bar(void);\nint stale(void);\n")
        plan = headers.plan(self.project, {"symbols": {"foo": "main/g1.h", "bar": "main/g2.h"}})
        data = next((data for path, data in plan.changed.items() if path.name == "g2.h"), b"int bar(void);\n")
        self.assertIn(b"bar", data)
