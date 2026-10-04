"""Planning and dealing using real split, draft and assignment storage."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from unbake.decomp import assign, drafts, plan
from unbake.project.config import Held, Policy, Project

KINDS = ("register", "order", "immediate", "relocation", "inserted", "missing", "changed")
BODY = bytes.fromhex("27bdffe0 afbf001c 00801021 8fbf001c 03e00008 27bd0020")


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project: Any = SimpleNamespace(
            id="00000000-0000-4000-8000-000000000001",
            checkout_id="00000000-0000-4000-8000-000000000002",
            name="fixture",
            root=self.root / "project",
            versions=("us", "eu"),
            names_from="us",
            version=self.version,
        )
        self.policy: Any = SimpleNamespace(state_root=self.root / "state", assignment_idle_hours=3)
        self.versions = {}
        for version in self.project.versions:
            directory = self.project.root / "versions" / version
            directory.mkdir(parents=True)
            self.versions[version] = SimpleNamespace(
                name=version,
                split=directory / "fixture.yaml",
                symbols=directory / "symbol_addrs.txt",
                baserom=self.project.root / f"baserom.{version}.z64",
            )
        self.store = drafts.Store(cast(Policy, self.policy), cast(Project, self.project))
        self.ledger = assign.Ledger(cast(Project, self.project), cast(Policy, self.policy))
        self.layout(
            [
                (name, BODY + bytes(length - len(BODY)), "asm", ())
                for name, length in (("alpha", 24), ("beta", 28), ("gamma", 32))
            ]
        )

    def version(self, name: str) -> SimpleNamespace:
        if name not in self.versions:
            raise Held("config", f"version {name}")
        return self.versions[name]

    def layout(self, rows: list[tuple[str, bytes, str, tuple[str, ...]]], *, version: str | None = None) -> None:
        for v in self.project.versions if version is None else (version,):
            facts = self.version(v)
            data = bytearray(16)
            text = (
                "name: fixture\nsegments:\n  - name: code\n    type: code\n"
                "    start: 0x10\n    vram: 0x80001000\n    subsegments:\n"
            )
            symbols = ""
            for name, body, kind, aliases in rows:
                start = len(data)
                text += f"      - [0x{start:X}, {kind}, {name}]\n"
                for alias in (name, *aliases):
                    symbols += f"{alias} = 0x{0x80001000 + start - 16:X};\n"
                data.extend(body)
            text += f"      - [0x{len(data):X}, data, pool]\n"
            data.extend(bytes(16))
            text += f"  - [0x{len(data):X}]\n"
            facts.split.write_text(text)
            facts.symbols.write_text(symbols)
            facts.baserom.write_bytes(data)

    def add(
        self,
        function: str,
        scores: dict[str, float],
        identical: bool = False,
        suffix: str = "",
    ) -> tuple[dict[str, Any], Path]:
        source = self.root / f"{function}.c"
        source.write_text(f"int {function}(void) {{ return 0; }} /* {suffix} */\n")
        trial: Any = SimpleNamespace(
            function=function,
            work_identity={"schema": 1, "project_id": self.project.id},
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            compares={
                v: SimpleNamespace(
                    version=v, identical=6 if identical else 5, of=6, typed=dict.fromkeys(KINDS, 0), lines=[]
                )
                for v in scores
            },
            preconditions=[],
            next_command="unbake decomp plan",
            identical_everywhere=identical,
        )
        digest = self.store.add(trial, source, scores)
        return cast(dict[str, Any], self.store.history()[-1]), self.store.root / digest / f"{function}.c"

    def history(self, rows: list[dict[str, Any]]) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / "trials.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_ranking_retains_identical_history_and_uses_weakest_score(self) -> None:
        cases: tuple[tuple[list[tuple[str, dict[str, float], bool]], list[str]], ...] = (
            (
                [("alpha", {"us": 99, "eu": 30}, False), ("beta", {"us": 75, "eu": 74}, False)],
                ["beta", "alpha", "gamma"],
            ),
            (
                [
                    ("gamma", {"us": 100, "eu": 100}, True),
                    ("gamma", {"us": 100, "eu": 100}, False),
                    ("alpha", {"us": 100, "eu": 100}, False),
                ],
                ["gamma", "alpha", "beta"],
            ),
            (
                [("gamma", {"us": 80, "eu": 80}, False), ("alpha", {"us": 80, "eu": 80}, False)],
                ["alpha", "gamma", "beta"],
            ),
            ([], ["alpha", "beta", "gamma"]),
        )
        for entries, expected in cases:
            with self.subTest(entries=entries):
                self.history([])
                for index, (function, scores, identical) in enumerate(entries):
                    self.add(function, scores, identical, str(index))
                rows = plan.ranked(self.project, self.policy)
                self.assertEqual([row.function for row in rows], expected)
                if entries and entries[0][2]:
                    self.assertTrue(rows[0].identical)
                    self.assertEqual(rows[0].score, 100)
                    assert rows[0].draft is not None
                    self.assertIn(b"/* 0 */", rows[0].draft.read_bytes())

    def test_latest_trial_for_same_source_replaces_old_evidence(self) -> None:
        row, path = self.add("beta", {"us": 100, "eu": 100}, True)
        replacement = copy.deepcopy(row)
        replacement["identical_everywhere"] = False
        replacement["compares"]["eu"]["identical"] = 5
        replacement["score"]["eu"] = 10
        self.history([row, replacement])
        result = plan.ranked(self.project, self.policy)[0]
        self.assertEqual((result.function, result.score, result.identical, result.draft), ("beta", 10, False, path))

    def test_plan_and_publish_select_the_same_exact_word_candidate(self) -> None:
        extra, _ = self.add("alpha", {"us": 99, "eu": 99}, suffix="extra instruction")
        exact, expected = self.add("alpha", {"us": 95, "eu": 94}, suffix="more words")
        extra["compares"]["us"]["typed"]["inserted"] = 1
        exact["compares"]["eu"]["identical"] = 6
        self.history([extra, exact])
        self.assertEqual(self.store.best("alpha"), expected)
        selected = next(row for row in plan.ranked(self.project, self.policy) if row.function == "alpha")
        self.assertEqual((selected.draft, selected.score), (expected, 94))

    def test_grouping_names_bytes_aliases_and_c_rows(self) -> None:
        for changed_words, matched, aliases in ((False, False, ()), (True, False, ("shared",)), (False, True, ())):
            with self.subTest(changed_words=changed_words, matched=matched):
                self.layout([("alpha", BODY, "asm", aliases)], version="us")
                self.layout(
                    [
                        (
                            "euro_alpha",
                            BODY[:-4] + bytes.fromhex("27bd0028") if changed_words else BODY,
                            "c" if matched else "asm",
                            aliases,
                        )
                    ],
                    version="eu",
                )
                self.history([])
                if not matched:
                    self.add("euro_alpha", {"us": 75, "eu": 70})
                rows = plan.ranked(self.project, self.policy)
                self.assertEqual(len(rows), 1)
                if rows:
                    self.assertEqual(rows[0].names, {"us": "alpha", "eu": "euro_alpha"})
                    self.assertEqual((rows[0].function, rows[0].score), ("alpha", None if matched else 70))
                    if matched:
                        self.assertEqual(rows[0].route, "port")
        self.layout([("alpha", BODY, "asm", ()), ("other", BODY, "asm", ())], version="us")
        self.layout([("euro_alpha", BODY, "asm", ())], version="eu")
        self.history([])
        self.assertEqual(len(plan.ranked(self.project, self.policy)), 3)

    def test_routes_from_small_words(self) -> None:
        for data, route in (
            (BODY, "drafter"),
            (BODY + BODY, "boundary"),
            (bytes(8), "boundary"),
            (bytes(4) + BODY, "boundary"),
            (BODY + bytes(8), "boundary"),
            (bytes.fromhex("03e00008"), "boundary"),
            (BODY + bytes.fromhex("24020001"), "boundary"),
            (bytes.fromhex("80001000 80002000"), "table"),
            (b"hello world\0", "table"),
            (bytes.fromhex("40026000 03e00008 00000000"), "asm"),
            (bytes.fromhex("bd000000 03e00008 00000000"), "asm"),
            (bytes.fromhex("00800008 00000000"), "drafter"),
            (bytes.fromhex("08000400 00000000"), "merge"),
        ):
            with self.subTest(data=data.hex()):
                self.assertEqual(plan.classify(data)[0], route)
        self.layout([("alpha", BODY + BODY, "asm", ())], version="eu")
        self.assertEqual(plan.ranked(self.project, self.policy)[0].route, "boundary")

    def test_dealing_uses_ranking_and_skips_open_version_aliases(self) -> None:
        self.add("beta", {"us": 100, "eu": 100}, True)
        for claimed, expected in ((None, ["beta", "alpha"]), ("beta", ["alpha", "gamma"])):
            with self.subTest(claimed=claimed):
                self.ledger.path.unlink(missing_ok=True)
                if claimed:
                    self.ledger.assign("other", "small", function=claimed)
                rows = plan.assign(self.project, self.policy, "worker", "small", count=2)
                self.assertEqual([row["function"] for row in rows], expected)
                self.assertEqual(len(self.ledger.open()), 2 + bool(claimed))
                self.assertFalse((self.project.root / "data").exists())
        self.ledger.path.unlink()
        self.assertEqual(len(plan.assign(self.project, self.policy, "worker", "small", count=20)), 3)
        with self.assertRaisesRegex(Held, "count: no unassigned"):
            plan.assign(self.project, self.policy, "worker", "small", count=1)
        self.ledger.path.write_text("{invalid}\n")
        with self.assertRaisesRegex(Held, "JSON"):
            plan.assign(self.project, self.policy, "worker", "small", count=1)

    def test_named_refusals_for_inputs_and_identity(self) -> None:
        for data in (b"", b"\0", b"\0" * 3, b"\0" * 5, None):
            with self.subTest(words=data), self.assertRaisesRegex(Held, "words"):
                plan.classify(cast(bytes, data))
        for field, value, label in (
            ("versions", (), "project.versions"),
            ("versions", ("us", "us"), "project.versions"),
            ("names_from", None, "project.names_from"),
            ("names_from", "jp", "project.names_from"),
        ):
            original = getattr(self.project, field)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(Held, label):
                setattr(self.project, field, value)
                plan.ranked(self.project, self.policy)
            setattr(self.project, field, original)
        for holder, tier, count, label in (
            (None, "small", 1, "holder"),
            ("worker", "", 1, "tier"),
            ("worker", "small", 0, "count"),
            ("worker", "small", True, "count"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(Held, label):
                plan.assign(self.project, self.policy, cast(str, holder), tier, count=count)
        self.layout([("alpha", BODY, "asm", ()), ("duplicate", BODY, "asm", ())], version="us")
        path = self.version("us").split
        path.write_text(path.read_text().replace(", duplicate]", ", folder/alpha]"))
        with self.assertRaisesRegex(Held, "ambiguous VERSION identity"):
            plan.ranked(self.project, self.policy)

    def test_named_refusals_for_retained_evidence(self) -> None:
        baseline, path = self.add("alpha", {"us": 80, "eu": 75})
        cases: list[tuple[str, Any, str]] = [
            ("function", None, "function"),
            ("source_sha256", "invalid", "source_sha256"),
            ("score", None, "score"),
            ("score", {"us": 80}, "VERSIONs"),
            ("score", {"us": True, "eu": 75}, "score"),
            ("score", {"us": float("nan"), "eu": 75}, "score"),
            ("score", {"us": 101, "eu": 75}, "score"),
            ("identical_everywhere", None, "identical_everywhere"),
            ("compares", None, "compares"),
        ]
        for field, value, label in cases:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(Held, label):
                row = copy.deepcopy(baseline)
                row[field] = value
                self.history([row])
                plan.ranked(self.project, self.policy)
        for field, value, label in (
            ("us", None, "compares"),
            ("identical", None, "identical"),
            ("of", 0, "of"),
            ("identical", 7, "exceeds of"),
            ("typed", None, "typed"),
            ("typed", {}, "typed.register"),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(Held, label):
                row = copy.deepcopy(baseline)
                if field == "us":
                    row["compares"]["us"] = value
                else:
                    row["compares"]["us"][field] = value
                self.history([row])
                plan.ranked(self.project, self.policy)
        for typed in (False, True):
            with self.subTest(typed=typed), self.assertRaisesRegex(Held, "identical_everywhere disagrees"):
                row = copy.deepcopy(baseline)
                row["identical_everywhere"] = True
                if typed:
                    row["compares"]["us"]["identical"] = 6
                    row["compares"]["us"]["typed"]["register"] = 1
                self.history([row])
                plan.ranked(self.project, self.policy)
        self.history([baseline])
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(Held, "source_sha256 differs"):
            plan.ranked(self.project, self.policy)
        path.unlink()
        with self.assertRaisesRegex(Held, "source"):
            plan.ranked(self.project, self.policy)


if __name__ == "__main__":
    unittest.main()
