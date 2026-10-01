"""Shared declaration preflight and final source regressions."""

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.decomp import needs
from unbake.layout.structs import layouts
from unbake.layout.structs_parser import Parser
from unbake.match import declarations
from unbake.match import queue as match
from unbake.project import build as project_build
from unbake.project.config import Held


class DeclarationTests(MatchFixture):
    def test_forward_typedef_spans_preserve_externs_and_function_body(self) -> None:
        for kind in ("struct", "union"):
            with self.subTest(kind=kind):
                forwards = [f"typedef {kind} {name} {name};" for name in ("First", "Second", "Third")]
                definitions = [f"{kind} {name} {{ int value; }};" for name in ("First", "Second", "Third")]
                extern = "extern Second *global;"
                body = "int alpha(First *arg) { return arg->value + global->value; }"
                text = "\n".join([*forwards, definitions[0], extern, *definitions[1:], body]) + "\n"
                parser = Parser(text)
                records = parser.parse()
                self.assertEqual(
                    [text[item.start : item.end] for item in parser.declarations],
                    [
                        *forwards,
                        *definitions,
                    ],
                )
                self.assertEqual(
                    [text[item.start : item.end] for item in records], [definition[:-1] for definition in definitions]
                )
                self.assertTrue(all(first.end <= second.start for first, second in pairwise(records)))
                final = declarations.final_source(self.project, text)
                self.assertIn(extern, final)
                self.assertIn(body, final)
                for declaration in (*forwards, *definitions):
                    self.assertNotIn(declaration, final)

    def test_shared_header_edits_acquire_and_check_unselected_versions(self) -> None:
        for outcome in ("success", "missing", "compare", "scoped"):
            with self.subTest(outcome=outcome):
                if outcome != "success":
                    self.doCleanups()
                    self.setUp()
                # The other cartridge already compiles this source, but its body
                # is not part of the queued function's trial proof.
                cartridge = self.project.version("eu")
                cartridge.split.write_text(cartridge.split.read_text().replace("asm, text/alpha", "c, alpha"))
                text = "struct Record { short x, y, z; };\nint alpha(void) { return 0; }\n"
                pending = self.pending(text)[:1] if outcome != "scoped" else []
                if outcome == "scoped":
                    text = "int alpha(void) { return 0; }\n"
                source = self.draft("alpha", text, versions=["us"], pending=pending)
                match.submit(self.project, self.policy, source, versions=("us",))
                if outcome == "missing":
                    self.project.build_link("eu").unlink()
                elif outcome == "compare":
                    self.build_failures.add(("alpha", "eu"))
                with patch.object(project_build, "build", wraps=self.build) as build:
                    receipts = match.run(self.project, self.policy)
                header = self.root / "include" / "structs.h"
                if outcome == "missing":
                    self.assertTrue(any("VERSION eu: cannot acquire generation" in line for line in receipts), receipts)
                    build.assert_not_called()
                else:
                    self.assertEqual(build.call_args.args[2], ["us"] if outcome == "scoped" else ["us", "eu"])
                if outcome in ("success", "scoped"):
                    self.assertTrue(any("alpha matched" in line for line in receipts), receipts)
                    self.assertEqual(self.matched()[0]["versions"], ["us"])
                    self.assertEqual(header.exists(), outcome == "success")
                    self.assertEqual(self.current(self.project, "eu").name, "eu.0" if outcome == "scoped" else "eu.1")
                else:
                    self.assertTrue(any("eu" in line and "HELD(match)" in line for line in receipts), receipts)
                    self.assertFalse(header.exists())
                    self.assertFalse((self.src / "alpha.c").exists())
                    self.assertEqual(len(self.queued()), 1)

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

    def test_header_promotion_keeps_queued_source_hash_through_retry(self) -> None:
        header = self.root / "include" / "structs.h"
        header.write_text("struct Record { char pad[4], tail[4]; };\n")
        text = "struct Record { int value; char tail[4]; };\nint alpha(void) { return 0; }\n"
        source = self.src / "alpha.c"
        source.write_text(text)
        self.prove(source, pending=self.pending(text))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        match.submit(self.project, self.policy, source)

        def inspect(tree: Path, generation_for: Callable[[str], Path]) -> None:
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertEqual(self.queued()[0]["source_sha256"], digest)
            self.assertIn("int value;", (tree / "include" / "structs.h").read_text())
            self.assertNotIn("struct Record", (tree / "src" / "alpha.c").read_text())

        self.on_build = inspect
        self.build_failures.add(("alpha", "us"))
        self.assertTrue(any("HELD(match)" in line for line in match.run(self.project, self.policy)))
        self.assertEqual(source.read_text(), text)
        self.build_failures.clear()
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("alpha matched" in line for line in receipts), receipts)
        self.assertEqual(self.matched()[0]["sha256"], digest)
        self.assertEqual(self.queued(), [])

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
