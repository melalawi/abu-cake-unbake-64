"""Publish judges the file's current text and refuses in plain words; rules a marker never waives; the recheck."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import land
from unbake.config import Held
from unbake.cycle import engine
from unbake.decomp import checks
from unbake.work import attempts

TEXT = "int alpha(void) { return 1; }\n"


def attempt(sha: str, *, percents: dict[str, float], exact: bool, best: float | None = None) -> attempts.Attempt:
    versions = {version: {"percent": value, "exact": value == 100} for version, value in percents.items()}
    return attempts.Attempt("t", "alpha", sha, 8, versions, best or min(percents.values()), exact, 0.1, "ido-7.1")


class ExactAttemptTests(TempCase):
    def test_every_way_a_publish_is_refused_or_allowed(self) -> None:
        file = self.root / "alpha.c"
        asm = 'int alpha(void) { __asm__("nop"); }\n'
        cases = [
            (
                "no attempt for this text",
                TEXT,
                lambda sha: [attempt("other", percents={"us": 100}, exact=True)],
                "land.not_compared",
            ),
            (
                "97.5 percent",
                TEXT,
                lambda sha: [attempt(sha, percents={"us": 100, "eu": 97.5}, exact=False)],
                r"land.not_exact: alpha matches only 97.50% \(lowest version eu\)",
            ),
            (
                "100 percent but the rules held it",
                asm,
                lambda sha: [attempt(sha, percents={"us": 100, "eu": 100}, exact=False)],
                "land.rules: alpha matches 100% but breaks the source rules: line 1: inline assembly is never allowed",
            ),
            ("exact", TEXT, lambda sha: [attempt(sha, percents={"us": 100}, exact=True)], None),
            (
                "an older exact attempt of other text does not count",
                TEXT,
                lambda sha: [
                    attempt("old", percents={"us": 100}, exact=True),
                    attempt(sha, percents={"us": 90}, exact=False),
                ],
                "land.not_exact",
            ),
        ]
        for label, text, build, refusal in cases:
            file.write_text(text)
            log = build(hashlib.sha256(text.encode()).hexdigest())
            with self.subTest(label), patch.object(attempts, "read", return_value=log):
                if refusal is None:
                    self.assertIs(land.exact_attempt(SimpleNamespace(), "alpha", file), log[-1])
                else:
                    with self.assertRaisesRegex(Held, refusal):
                        land.exact_attempt(SimpleNamespace(), "alpha", file)


class RuleTests(TempCase):
    def test_a_marker_waives_neither_volatile_nor_inline_asm(self) -> None:
        marker = "/* FAKEMATCH: measured scheduling */\n"
        cases = [
            ("inline asm with a marker", 'void f(void) { __asm__("nop"); }\n', 1),
            ("volatile with a marker", "volatile int x;\n", 1),
        ]
        for label, body, unmarked in cases:
            with self.subTest(label):
                self.assertEqual(len(checks.unmarked(marker + body)), unmarked)

    def test_every_rule_a_finding_can_carry_has_a_sentence(self) -> None:
        emitted = {rule.id for rule in checks.RULES} - {"shared-declarations"}
        emitted |= {"local-type-copy", "local-gbi-macro", "invented-struct", "symbol-alias"}
        self.assertEqual(set(checks.SENTENCE), emitted)

    def test_plain_reads_as_a_sentence(self) -> None:
        finding = checks.GuardFinding("inline-asm", 3, '__asm__("nop");', None)
        self.assertEqual(checks.plain(finding), 'line 3: inline assembly is never allowed (__asm__("nop");)')


class RecheckTests(TempCase):
    def recheck(self, built: bytes, rows: list[SimpleNamespace]) -> dict:
        project = SimpleNamespace(src=self.root / "src")
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/merged.c").write_text(TEXT)
        with (
            patch("unbake.config.load", return_value=project),
            patch("unbake.layout.split.member_owners", return_value={"us": rows[0]}),
            patch("unbake.runner.build_unit", return_value=built),
            patch("unbake.layout.split.words", return_value=b"\x01\x02\x03\x04"),
        ):
            return engine._recheck_task((self.root, SimpleNamespace(), "alpha"))

    def test_a_function_inside_a_merged_unit_is_exact_and_a_difference_is_plain(self) -> None:
        rows = [SimpleNamespace(path="merged", kind="c")]
        self.assertEqual(
            self.recheck(b"\x01\x02\x03\x04", rows), {"exact": True, "best_percent": 100.0, "diagnostic": ""}
        )
        result = self.recheck(b"\x00\x00\x00\x00", rows)
        self.assertEqual(
            (result["exact"], result["best_percent"], result["diagnostic"]),
            (False, None, "alpha no longer builds identical in us (unit merged)"),
        )
        self.assertIsInstance(Path(rows[0].path), Path)
