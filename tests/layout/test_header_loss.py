"""Every shared header writer refuses declaration loss before publication."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.fold import apply as fold_apply
from unbake.layout import apply, header_loss, header_step
from unbake.layout.split import Edit
from unbake.typemap import database


class HeaderLossTests(ProjectCase):
    def setUp(self):
        super().setUp()
        from unbake.config import Host

        values = {name: dict(row) for name, row in self.host.values.items()}
        values["resources"].update(
            memory_total_bytes=4 << 30, memory_parent_bytes=1 << 30, memory_worker_bytes=512 << 20
        )
        self.host = Host.from_values(values, "draft")
        self.header = self.project.include[0] / "used.h"
        self.header.write_text(
            "typedef int Word;\nstruct Vec3 { Word x; };\ntypedef struct Vec3 Vec3;\nextern Vec3 point;\n"
        )
        self.source = self.project.src / "nested/consumer.c"
        self.source.parent.mkdir()
        self.source.write_text('#include "used.h"\nint consumer(void) { Vec3 p = point; return p.x; }\n')

    def test_typedef_is_not_satisfied_by_a_same_named_tag(self):
        output = self.header.read_bytes().replace(b"typedef struct Vec3 Vec3;\n", b"")
        with self.assertRaisesRegex(Held, r"would remove Vec3 used by published C .*nested/consumer.c"):
            header_loss.check(self.project, {self.header: output})

    def test_transitive_member_type_definition_and_global_are_retained(self):
        for declaration, name in (
            (b"typedef int Word;\n", "Word"),
            (b"struct Vec3 { Word x; };\n", "struct Vec3"),
            (b"extern Vec3 point;\n", "point"),
        ):
            with self.subTest(name=name):
                output = self.header.read_bytes().replace(declaration, b"")
                with self.assertRaisesRegex(Held, "would remove " + name + r" used by published C .*consumer.c"):
                    header_loss.check(self.project, {self.header: output})

    def test_a_reachable_move_keeps_all_declarations(self):
        destination = self.header.with_name("moved.h")
        output = {destination: self.header.read_bytes(), self.header: b'#include "moved.h"\n'}
        header_loss.check(self.project, output)
        header_loss.check(
            self.project,
            {
                destination: self.header.read_bytes(),
                self.source: self.source.read_bytes().replace(b"used.h", b"moved.h"),
            },
            obsolete={self.header},
        )

    def test_unreachable_move_cannot_mask_loss(self):
        destination = self.header.with_name("moved.h")
        with self.assertRaisesRegex(Held, r"consumer.c: would lose reachable declarations .*Vec3"):
            header_loss.check(self.project, {destination: self.header.read_bytes(), self.header: b""})

    def test_unchanged_header_can_supply_a_removed_duplicate(self):
        duplicate = self.header.with_name("duplicate.h")
        duplicate.write_bytes(self.header.read_bytes())
        header_loss.check(self.project, {duplicate: b""})

    def test_moves_resolve_all_include_roots_and_staged_paths(self):
        other = self.root / "extra_include"
        other.mkdir()
        destination = other / "moved.h"
        staged = self.root / "staged.h"
        staged.write_bytes(self.header.read_bytes())
        header_loss.check(
            replace(self.project, work_include=(other,)),
            {
                self.header: b"",
                destination: staged,
                self.source: self.source.read_bytes().replace(b"used.h", b"moved.h"),
            },
        )

    def test_removing_unused_declarations_is_allowed(self):
        self.header.write_text(self.header.read_text() + "typedef int Unused;\n")
        header_loss.check(self.project, {self.header: self.header.read_bytes().replace(b"typedef int Unused;\n", b"")})

    def test_layout_install_and_plan_refuse_before_writing_or_deleting(self):
        output = {self.header: b"", self.project.src / "new.c": b"void new(void) {}\n"}
        before = self.header.read_bytes()
        for call in (apply.install, header_step.plan):
            with self.subTest(writer=call.__name__):
                with self.assertRaisesRegex(Held, r"Vec3.*consumer.c"):
                    if call is header_step.plan:
                        call(self.project, output, self.host)
                    else:
                        call(self.project, output)
                self.assertEqual(self.header.read_bytes(), before)
                self.assertFalse((self.project.src / "new.c").exists())
        with patch.object(apply.index, "headers", return_value={self.header}):
            with self.assertRaisesRegex(Held, r"Vec3.*consumer.c"):
                apply.install(self.project, {})
            self.assertEqual(self.header.read_bytes(), before)

    def test_types_publisher_checks_even_cached_outputs_before_staging(self):
        session = SimpleNamespace(sources={}, render=lambda *args: {self.header: b""})
        with (
            patch.object(database.regeneration, "Session", return_value=session),
            patch.object(database.types_db, "stage") as stage,
            patch.object(database.storage, "write") as write,
            self.assertRaisesRegex(Held, r"Vec3.*consumer.c"),
        ):
            database.publish(self.project, {}, {})
        stage.assert_not_called()
        write.assert_not_called()

    def test_fold_checks_its_complete_outputs_before_private_writes(self):
        edits = [
            Edit(self.header, self.header.read_text(), "", ("us",)),
            Edit(self.source, "", self.source.read_text(), ("us",)),
        ]
        with (
            patch.object(fold_apply, "view", return_value=self.project),
            patch.object(fold_apply.gbi, "microcode"),
            patch.object(fold_apply.gbi, "prepare", return_value=SimpleNamespace(source="", headers=set())),
            patch.object(fold_apply.declarations, "folded_edits", return_value=edits),
            patch.object(fold_apply.checks, "run", return_value=[]),
            self.assertRaisesRegex(Held, r"Vec3.*consumer.c"),
        ):
            fold_apply.fold(self.project, self.host, "nested/consumer", "", versions=("us",))
        self.assertIn("typedef struct Vec3 Vec3;", self.header.read_text())
