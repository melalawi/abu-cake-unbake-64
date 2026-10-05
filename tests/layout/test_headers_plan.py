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

    def plan(self, outputs: dict, generated: frozenset = frozenset()) -> dict:
        with patch.object(header_step.index, "headers", return_value=generated):
            return header_step.plan(self.project, outputs)

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

    def test_merge_only_refuses_deleting_a_header_with_a_used_name(self) -> None:
        with self.assertRaisesRegex(Held, r"headers.merge_only: .*g.h: would remove foo used by published C"):
            self.plan({}, frozenset({self.header}))

    def test_every_refusal_is_listed_once(self) -> None:
        other = self.header.with_name("h.h")
        other.write_text("int bar(void);\n")
        (self.project.src / "beta.c").write_text("int beta(void) { return bar(); }\n")
        with self.assertRaisesRegex(Held, r"merge_only: .*g.h: would remove foo .*; .*h.h: would remove bar "):
            self.plan({self.header: b"", other: b""})

    def test_a_function_a_source_only_defines_is_not_a_use(self) -> None:
        for body, refused in [
            ("void own(int a) {\n}\n", False),
            ("void own(int a);\nvoid own(int a) {\n}\n", True),
            ("void own(int a) {\n    own(a);\n}\n", True),
        ]:
            with self.subTest(body=body):
                (self.project.src / "own.c").write_text(body)
                self.header.write_text("int foo(void);\nvoid own(int a);\n")
                if refused:
                    with self.assertRaisesRegex(Held, r"would remove own used"):
                        self.plan({self.header: b"int foo(void);\n"})
                else:
                    self.assertEqual(len(self.plan({self.header: b"int foo(void);\n"})), 1)

    def test_run_installs_every_output_so_unchanged_headers_are_kept(self) -> None:
        """apply.install deletes generated headers absent from what it is given; give it all outputs."""
        other = self.header.with_name("h.h")
        outputs = {self.header: self.header.read_bytes(), other: b"int bar(void);\n"}
        with (
            patch.object(header_step.apply, "units"),
            patch.object(header_step.apply, "render", return_value=outputs),
            patch.object(header_step.index, "headers", return_value=frozenset({self.header})),
            patch.object(header_step, "validate"),
            patch.object(header_step.apply, "install") as install,
        ):
            self.assertEqual(header_step.run(self.project, self.host), [other])
        install.assert_called_once_with(self.project, outputs)
