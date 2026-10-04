"""Authored C cleanup only changes editable source and its header overlay."""

from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.decomp import cleanup, gbi, work
from unbake.project.config import Held


class CleanupTests(MatchFixture):
    def setUp(self) -> None:
        super().setUp()
        context = patch.object(cleanup.type_context, "required", return_value=("d" * 64, ""))
        context.start()
        self.addCleanup(context.stop)
        self.source = self.project.drafts / "alpha/alpha.c"
        self.source.parent.mkdir(parents=True)

    def test_canonical_scalar_copy_is_removed_without_live_header_edits(self) -> None:
        header = self.project.include[0] / "types.h"
        header.write_text("typedef int s32;\n")
        self.source.write_text("typedef int s32;\ns32 alpha(void) { return 0; }\n")
        before = header.read_bytes()
        cleanup.prepare(self.project, self.policy, self.source)
        self.assertNotIn("typedef", self.source.read_text())
        self.assertIn('#include "types.h"', self.source.read_text())
        self.assertEqual(header.read_bytes(), before)
        self.assertEqual(work.overlay_data(self.project, self.source)["edits"], {})
        self.assert_untouched()

    def test_verified_gbi_macro_copy_moves_to_the_staged_open_header(self) -> None:
        definition = next(line for line in gbi.HEADER.read_text().splitlines() if line.startswith("#define _SHIFTL("))
        self.source.write_text(definition + "\nint alpha(void) { return _SHIFTL(1, 0, 8); }\n")
        cleanup.prepare(self.project, self.policy, self.source)
        self.assertNotIn("#define _SHIFTL", self.source.read_text())
        self.assertIn('#include "gbi.h"', self.source.read_text())
        self.assertFalse((self.project.include[0] / "gbi.h").exists())
        self.assertIn("include/gbi.h", work.overlay_data(self.project, self.source)["edits"])

    def test_authored_aggregate_moves_into_only_the_staged_shared_header(self) -> None:
        self.source.write_text("struct Record { int value; };\nint alpha(void) { return 0; }\n")
        cleanup.prepare(self.project, self.policy, self.source)
        self.assertNotIn("struct Record {", self.source.read_text())
        self.assertIn('#include "main/alpha.h"', self.source.read_text())
        self.assertFalse((self.project.include[0] / "main/alpha.h").exists())
        edits = work.overlay_data(self.project, self.source)["edits"]
        self.assertIn("include/main/alpha.h", edits)
        self.assert_untouched()

    def test_conflicting_type_or_unrepresentable_raw_packet_preserves_editable_bytes(self) -> None:
        (self.project.include[0] / "types.h").write_text("typedef int s32;\n")
        for text, key in (
            ("typedef unsigned int s32;\nint alpha(void) { return 0; }\n", "conflicting draft scalar typedef"),
            ("void alpha(void) { p->words.w0 = raw; }\n", "raw-gfx"),
        ):
            with self.subTest(key=key):
                self.source.write_text(text)
                with self.assertRaisesRegex(Held, key):
                    cleanup.prepare(self.project, self.policy, self.source)
                self.assertEqual(self.source.read_text(), text)
                self.assertFalse((self.source.parent / "overlay.json").exists())
                self.assert_untouched()

    def test_layout_rename_does_not_hide_a_conflicting_canonical_scalar_alias(self) -> None:
        (self.project.include[0] / "types.h").write_text("typedef unsigned char u8;\n")
        text = "typedef struct Other {int value;} u8;\nint alpha(void) {return 0;}\n"
        self.source.write_text(text)
        with self.assertRaisesRegex(Held, "u8: conflicting draft scalar typedef"):
            cleanup.prepare(self.project, self.policy, self.source)
        self.assertEqual(self.source.read_text(), text)
        self.assert_untouched()
