"""Shared declaration preflight and final source regressions."""

from dataclasses import asdict

from tests.match.support import MatchFixture
from unbake.decomp import needs
from unbake.layout.structs import layouts
from unbake.match import declarations
from unbake.match import queue as match
from unbake.project.config import Held


class DeclarationTests(MatchFixture):
    def pending(self, text: str) -> list[needs.Need]:
        record = layouts(text)[0]
        return [
            needs.LayoutNeed(
                version,
                record.name,
                [asdict(member) for member in record.fields],
                str(self.sources / "alpha.c"),
                dict(kind=record.kind, size=record.size, alignment=record.alignment, aliases=list(record.aliases)),
            )
            for version in self.versions
        ]

    def test_conflict_is_refused_before_staging_or_build(self) -> None:
        header = self.root / "include" / "structs.h"
        header.write_text("struct Record { int value; };\n")
        text = "struct Record { short value; };\nint alpha(void) { return 0; }\n"
        source = self.draft("alpha", text, pending=self.pending(text))
        with self.assertRaisesRegex(Held, "Record.value"):
            match.submit(self.project, self.policy, source)
        with self.assertRaisesRegex(Held, "Record.value"):
            declarations.preflight(self.project, self.policy, self.pending(text))
        header.write_text("struct Record { short value; };\n")
        match.submit(self.project, self.policy, source)
        header.write_text("struct Record { int value; };\n")
        self.assertTrue(any("Record.value" in line for line in match.run(self.project, self.policy)))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "build" / "match").exists())

    def test_landing_creates_and_extends_one_shared_home(self) -> None:
        for function, name in (("alpha", "Record"), ("beta", "Other")):
            text = (
                f"typedef struct {name} {{ char pad[4]; int value; }} {name};\nint {function}(void) {{ return 0; }}\n"
            )
            source = self.draft(function, text, pending=self.pending(text))
            match.submit(self.project, self.policy, source)
            self.assertTrue(any(f"{function} matched" in line for line in match.run(self.project, self.policy)))
            landed = (self.src / source.name).read_text()
            self.assertIn('#include "structs.h"', landed)
            self.assertNotIn("typedef struct", landed)
        header = (self.root / "include" / "structs.h").read_text()
        self.assertIn("struct Record", header)
        self.assertIn("struct Other", header)
        self.assertFalse((self.root / "include" / "alpha.h").exists())

    def test_landing_removes_draft_marker_and_preserves_other_comments(self) -> None:
        text = (
            "/* NON_MATCHING: draft of alpha; verify behavior and bytes before match. */\n"
            "/* purpose */\nint alpha(void) { return 0; }\n"
        )
        source = self.draft("alpha", text)
        match.submit(self.project, self.policy, source)
        self.assertTrue(any("alpha matched" in line for line in match.run(self.project, self.policy)))
        self.assertEqual((self.src / "alpha.c").read_text(), "/* purpose */\nint alpha(void) { return 0; }\n")
        self.assertEqual(source.read_text(), text)
