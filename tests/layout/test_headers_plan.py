"""The headers step plan: only changed bytes, and never removing a name that published C spells."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout import header_step


class HeaderPlanTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.header = self.project.include[-1] / "main" / "g.h"
        self.header.parent.mkdir()
        self.header.write_text("int foo(void);\nint stale(void);\n")
        (self.project.src / "alpha.c").write_text('#include "main/g.h"\nint alpha(void) { return foo(); }\n')

    def plan(self, outputs: dict) -> dict:
        with patch.object(header_step.apply, "render", return_value=outputs):
            return header_step.plan(self.project, self.host)

    def test_unchanged_outputs_plan_nothing(self) -> None:
        self.assertEqual(self.plan({self.header: self.header.read_bytes()}), {})

    def test_new_and_changed_files_are_planned(self) -> None:
        other = self.header.with_name("h.h")
        changed = {self.header: b"int foo(void);\nint stale(void);\nint more(void);\n", other: b"int bar(void);\n"}
        self.assertEqual(self.plan(changed), changed)

    def test_merge_only_refuses_removing_a_used_name(self) -> None:
        with self.assertRaisesRegex(Held, r"headers.merge_only: .*g.h: would remove foo used by published C"):
            self.plan({self.header: b"int stale(void);\n"})

    def test_unused_name_may_leave_a_header(self) -> None:
        self.assertEqual(self.plan({self.header: b"int foo(void);\n"}), {self.header: b"int foo(void);\n"})
