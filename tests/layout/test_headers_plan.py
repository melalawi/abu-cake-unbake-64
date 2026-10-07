"""The headers step plan: only changed bytes, and never removing a name that published C spells."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout import header_step


class HeaderPlanTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        from unbake.config import Host

        values = {name: dict(row) for name, row in self.host.values.items()}
        values["resources"].update(
            memory_total_bytes=4 << 30, memory_parent_bytes=1 << 30, memory_worker_bytes=512 << 20
        )
        self.host = Host.from_values(values, "draft")
        self.header = self.project.include[-1] / "main" / "g.h"
        self.header.parent.mkdir()
        self.header.write_text("int foo(void);\nint stale(void);\n")
        (self.project.src / "alpha.c").write_text('#include "main/g.h"\nint alpha(void) { return foo(); }\n')

    def plan(self, outputs: dict, generated: frozenset = frozenset()) -> dict:
        with patch.object(header_step.index, "headers", return_value=generated):
            return header_step.plan(self.project, outputs, self.host)[0]

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
        self.assertEqual(install.call_count, 1)
        self.assertEqual(install.call_args.args, (self.project, outputs))
        self.assertTrue(
            install.call_args.kwargs["check"].valid(
                __import__("unbake.project.headers", fromlist=["Graph"]).Graph.capture(self.project).view,
                __import__("unbake.layout.header_loss", fromlist=["output_key"]).output_key(outputs, ()),
            )
        )


class HeaderRunTests(ProjectCase):
    def test_a_hold_after_writing_leaves_the_tree_byte_identical(self) -> None:
        header = self.project.include[-1] / "main" / "g.h"
        header.parent.mkdir()
        header.write_text("int foo(void);\n")
        split = self.project.version("us").split
        before = {path: path.read_bytes() for path in (header, split)}
        new = header.with_name("h.h")

        def units(project: object) -> None:
            split.write_text(split.read_text() + "# merged pools\n")

        def install(project: object, outputs: dict, *, check=None) -> None:
            header.write_text("int moved(void);\n")  # one file written, then the process holds
            raise Held("headers", "headers.declaration: refused")

        outputs = {header: b"int moved(void);\n", new: b"int foo(void);\n"}
        with (
            patch.object(header_step.apply, "units", side_effect=units),
            patch.object(header_step.apply, "render", return_value=outputs),
            patch.object(header_step.index, "headers", return_value=frozenset({header})),
            patch.object(header_step, "validate"),
            patch.object(header_step.apply, "install", side_effect=install),
            self.assertRaises(Held),
        ):
            header_step.run(self.project, self.host)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(new.exists())
        self.assertFalse(header_step.journal_path(self.project).exists())


class ReachableTypeTests(ProjectCase):
    def test_a_type_that_moved_is_included_from_its_new_home(self) -> None:
        from types import SimpleNamespace

        from unbake.layout import apply

        owner = SimpleNamespace(header="span_16E000/code_80405454.h")
        ownership = SimpleNamespace(owners={"func_804085E0_de": owner})
        lookup = {
            "headers": {"span_16E000/code_80405454.h": "a", "span_16E000/code_80405DC0.h": "b"},
            "symbols": {"Menu_func_804085E0_de": "span_16E000/code_80405DC0.h"},
        }
        text = '#include "span_16E000/code_80405454.h"\nvoid func_804085E0_de(Menu_func_804085E0_de *menu) {\n}\n'
        result = apply.rewrite(
            self.project.src / "func_804085E0_de.c", text, "func_804085E0_de", ownership, lookup, previous=set()
        )
        self.assertIn('#include "span_16E000/code_80405DC0.h"', result)
        self.assertIn('#include "span_16E000/code_80405454.h"', result)
