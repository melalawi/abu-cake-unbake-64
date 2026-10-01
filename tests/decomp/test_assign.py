"""Named claims and host ledger state against real split and draft readers."""

import hashlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.layout.test_split import ProjectFixture
from unbake.decomp import assign, drafts
from unbake.layout import split
from unbake.project.config import Held


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.project = ProjectFixture(Path(self.temp.name))
        self.project.names_from = "us"
        self.store = drafts.Store(self.project.policy, self.project)
        self.ledger = assign.Ledger(self.project, self.project.policy)

    def record(
        self,
        function: str = "alpha",
        *,
        at: str | None = None,
        identifier: str = "seed",
        names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        versions = list(self.project.versions)
        return {
            "id": identifier,
            "function": function,
            "versions": versions,
            "names": names or {v: function for v in versions},
            "holder": "worker",
            "tier": "small",
            "at": at or datetime.now(UTC).isoformat(),
        }

    def seed(self, rows: list[dict[str, Any]]) -> None:
        self.ledger.path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def add_activity(self, function: str) -> None:
        source = self.project.root / f"{function}.c"
        source.write_text(f"void {function}(void) {{}}\n")
        trial = SimpleNamespace(
            function=function,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            compares={
                v: SimpleNamespace(
                    version=v,
                    identical=1,
                    of=2,
                    typed=dict.fromkeys(
                        ("register", "order", "immediate", "relocation", "inserted", "missing", "changed"), 0
                    ),
                    lines=[],
                )
                for v in self.project.versions
            },
            needs=[],
            preconditions=[],
            next_command="unbake decomp plan",
            identical_everywhere=False,
        )
        self.store.add(trial, source, dict.fromkeys(self.project.versions, 50))

    def test_holder_and_tier_required_before_writing(self) -> None:
        for holder, tier, label in (
            (None, "small", "holder"),
            ("", "small", "holder"),
            ("worker", None, "tier"),
            ("worker", " ", "tier"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(Held, label):
                self.ledger.assign(holder, tier, function="alpha")
        self.assertFalse(self.ledger.path.exists())

    def test_required_policy_and_identity_are_named(self) -> None:
        for field, value, label in (
            ("state_root", None, "policy.state_root"),
            ("assignment_idle_hours", None, "assignment_idle_hours"),
            ("assignment_idle_hours", True, "assignment_idle_hours"),
            ("assignment_idle_hours", 0, "assignment_idle_hours"),
            ("assignment_idle_hours", float("nan"), "assignment_idle_hours"),
        ):
            policy = SimpleNamespace(**vars(self.project.policy))
            setattr(policy, field, value)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(Held, label):
                assign.Ledger(self.project, policy)
        for name in (None, "", "..", "a/b"):
            with self.subTest(name=name), self.assertRaisesRegex(Held, "project.name"):
                assign.Ledger(SimpleNamespace(name=name), self.project.policy)
        for function in (None, "", " "):
            with self.subTest(function=function), self.assertRaisesRegex(Held, "function"):
                self.ledger.assign("worker", "small", function=function)

    def test_specific_function_covers_every_version(self) -> None:
        row = self.ledger.assign("worker", "small", function="beta")[0]
        self.assertEqual(row["function"], "beta")
        self.assertEqual(row["names"], {"us": "beta", "eu": "beta"})
        self.assertEqual(row["versions"], ["us", "eu"])
        with self.assertRaisesRegex(Held, "unassigned unmatched"):
            self.ledger.assign("other", "small", function="beta")
        lines = self.ledger.path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), row)

    def test_shared_name_groups_versions_with_different_words(self) -> None:
        rom = self.project.version("eu").baserom
        content = bytearray(rom.read_bytes())
        content[0x10:0x20] = b"\xff" * 0x10
        rom.write_bytes(content)
        row = self.ledger.assign("worker", "small", function="alpha")[0]
        self.assertEqual(row["versions"], ["us", "eu"])

    def test_equal_words_group_different_version_names_and_block_alias(self) -> None:
        self.project.layout(
            "eu", [(0x10, "asm", "euro_alpha"), (0x20, "asm", "beta"), (0x38, "asm", "gamma"), (0x40, "data", "pool")]
        )
        row = self.ledger.assign("worker", "small", function="alpha")[0]
        self.assertEqual(row["names"], {"us": "alpha", "eu": "euro_alpha"})
        with self.assertRaisesRegex(Held, "unassigned"):
            self.ledger.assign("other", "small", function="euro_alpha")

    def test_explicit_other_version_alias_is_retained_as_function(self) -> None:
        self.project.layout(
            "eu", [(0x10, "asm", "euro_alpha"), (0x20, "asm", "beta"), (0x38, "asm", "gamma"), (0x40, "data", "pool")]
        )
        row = self.ledger.assign("worker", "small", function="euro_alpha")[0]
        self.assertEqual(row["function"], "euro_alpha")
        self.assertEqual(row["names"]["us"], "alpha")

    def test_actual_symbol_name_can_differ_from_yaml_stem(self) -> None:
        version = self.project.version("eu")
        version.symbols.write_text(split.read(version.symbols).replace("alpha =", "euro_alpha ="))
        row = self.ledger.assign("worker", "small", function="alpha")[0]
        self.assertEqual(row["names"], {"us": "alpha", "eu": "euro_alpha"})

    def test_c_rows_are_excluded_without_tracking_files(self) -> None:
        self.project.layout(
            "us", [(0x10, "c", "alpha"), (0x20, "c", "beta"), (0x38, "asm", "gamma"), (0x40, "data", "pool")]
        )
        for name in ("alpha", "beta", "pool", "missing"):
            with self.subTest(name=name), self.assertRaisesRegex(Held, "unassigned unmatched"):
                self.ledger.assign("other", "small", function=name)

    def test_idle_assignment_expires_without_sleeping(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        self.seed([self.record(at=old)])
        self.assertEqual(self.ledger.open(), [])
        row = self.ledger.assign("new_worker", "small", function="alpha")[0]
        self.assertEqual(len(self.ledger.path.read_text().splitlines()), 2)
        self.assertEqual(self.ledger.open(), [row])

    def test_recent_draft_activity_keeps_old_assignment_open(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        row = self.record(at=old)
        self.seed([row])
        self.add_activity("alpha")
        self.assertEqual(self.ledger.open(), [row])
        with self.assertRaisesRegex(Held, "unassigned"):
            self.ledger.assign("new_worker", "small", function="alpha")

    def test_release_appends_event_and_frees_all_versions(self) -> None:
        row = self.ledger.assign("worker", "small", function="alpha")[0]
        original = self.ledger.path.read_text()
        self.assertIsNone(self.ledger.release(row["id"]))
        self.assertEqual(self.ledger.open(), [])
        after = self.ledger.path.read_text()
        self.assertTrue(after.startswith(original))
        event = json.loads(after.splitlines()[-1])
        self.assertEqual(event["event"], "release")
        self.assertEqual(event["id"], row["id"])
        replacement = self.ledger.assign("new_worker", "large", function="alpha")[0]
        self.assertNotEqual(replacement["id"], row["id"])
        self.assertEqual(self.ledger.open(), [replacement])

    def test_release_refuses_missing_unknown_and_expired_id(self) -> None:
        for value in (None, "", "missing"):
            with self.subTest(value=value), self.assertRaisesRegex(Held, "assignment_id"):
                self.ledger.release(value)
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        self.seed([self.record(at=old)])
        with self.assertRaisesRegex(Held, "no open assignment"):
            self.ledger.release("seed")

    def test_ledger_rejects_invalid_json_and_missing_fields_by_name(self) -> None:
        self.ledger.path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.path.write_text("{invalid}\n")
        with self.assertRaisesRegex(Held, "JSON"):
            self.ledger.open()
        for key in ("id", "function", "holder", "tier", "at", "names", "versions"):
            row = self.record()
            del row[key]
            self.seed([row])
            with self.subTest(key=key), self.assertRaisesRegex(Held, key):
                self.ledger.open()

    def test_missing_timestamp_on_draft_activity_is_held(self) -> None:
        self.seed([self.record()])
        self.add_activity("alpha")
        path = self.store.root / "trials.jsonl"
        row = json.loads(path.read_text())
        del row["at"]
        path.write_text(json.dumps(row) + "\n")
        with self.assertRaisesRegex(Held, "at"):
            self.ledger.open()

    def test_nonexistent_ledger_open_does_not_create_file(self) -> None:
        self.assertEqual(self.ledger.open(), [])
        self.assertFalse(self.ledger.path.exists())
        self.ledger.assign("worker", "small", function="alpha")
        self.assertEqual(self.ledger.path, self.project.policy.state_root / self.project.name / "assignments.jsonl")
        self.assertFalse((self.project.root / "data").exists())

    def test_append_preserves_valid_unterminated_final_record(self) -> None:
        row = self.record()
        self.seed([row])
        self.ledger.path.write_text(self.ledger.path.read_text().rstrip("\n"))
        new = self.ledger.assign("worker", "small", function="beta")[0]
        self.assertEqual(self.ledger.open(), [row, new])

    def test_duplicate_same_version_alias_is_refused(self) -> None:
        path = self.project.version("us").symbols
        path.write_text(split.read(path) + "extra = 0x80001000;\n")
        yaml = self.project.version("us").split
        yaml.write_text(split.read(yaml).replace(", beta]", ", extra]"))
        with self.assertRaisesRegex(Held, "ambiguous VERSION identity"):
            self.ledger.assign("worker", "small", function="alpha")


if __name__ == "__main__":
    unittest.main()
