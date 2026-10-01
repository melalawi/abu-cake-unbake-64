"""Match behavior with stored trial proofs and a controlled build implementation."""

import hashlib
from collections.abc import Callable
from pathlib import Path

from tests.match.support import MatchFixture
from unbake.decomp import drafts, needs
from unbake.match import queue as match
from unbake.project.config import Held


class MatchTests(MatchFixture):
    def test_failed_match_names_actual_rom_difference_before_discard(self) -> None:
        expected = bytes(0x1040)
        for version in self.versions:
            cartridge = self.project.version(version)
            cartridge.baserom.write_bytes(expected)
            cartridge.split.write_text(
                cartridge.split.read_text().replace("segments:\n", "segments:\n  - [0, header, header]\n")
            )
        self.build_failures.add(("alpha", "us"))
        offset = 0x1012

        def produce(tree: Path, generation_for: Callable[[str], Path]) -> None:
            for version in self.versions:
                produced = bytearray(expected)
                if version == "us":
                    produced[offset] = 0xAB
                (generation_for(version) / f"fixture.{version}.z64").write_bytes(produced)

        self.on_build = produce
        self.queue("alpha")
        for offset, owner in ((0x1012, "beta"), (0x1030, "constants"), (0x20, "header")):
            with self.subTest(offset=offset):
                receipts = match.run(self.project, self.policy)
                self.assertEqual(len(receipts), 1)
                self.assertIn(f"VERSION us: first differing ROM offset 0x{offset:X}", receipts[0])
                self.assertIn("expected " + bytes(16).hex(), receipts[0])
                self.assertIn("produced ab" + bytes(15).hex() + f"; unit {owner}", receipts[0])
                self.assertEqual(list((self.root / "build").glob("us.[1-9]*")), [])
                self.assertEqual(len(self.queued()), 1)

    def test_failed_build_with_unchanged_rom_keeps_build_reason(self) -> None:
        self.build_failures.add(("alpha", "us"))

        def produce(tree: Path, generation_for: Callable[[str], Path]) -> None:
            for version in self.versions:
                content = self.project.version(version).baserom.read_bytes()
                (generation_for(version) / f"fixture.{version}.z64").write_bytes(content)

        self.on_build = produce
        self.queue("alpha")
        receipts = match.run(self.project, self.policy)
        self.assertIn("VERSION us: ROM bytes agree; compare", receipts[0])
        self.assertEqual(len(self.queued()), 1)

    def test_submit_status_withdraw_and_replacement(self) -> None:
        source = self.draft("alpha")
        self.assertIn("alpha queued", match.submit(self.project, self.policy, source)[0])
        self.assertIn(str(source), match.status(project=self.project, policy=self.policy)[0])
        match.submit(self.project, self.policy, source)
        self.assertEqual(len(self.queued()), 1)
        source = self.draft("alpha", "int alpha(void) { return 1; }\n")
        match.submit(self.project, self.policy, source)
        self.assertEqual(self.queued()[0]["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertIn("alpha withdrawn", match.withdraw("alpha", project=self.project, policy=self.policy)[0])
        self.assertEqual(match.status(project=self.project, policy=self.policy), [])
        with self.assertRaisesRegex(Held, "alpha.*not in"):
            match.withdraw("alpha", project=self.project, policy=self.policy)
        self.assertEqual(self.calls, [])

    def test_submit_refuses_missing_or_nonidentical_exact_sha_proof(self) -> None:
        source = self.draft("alpha", identical=False)
        with self.assertRaisesRegex(Held, "alpha.*identical_everywhere"):
            match.submit(self.project, self.policy, source)
        self.remove_proofs("alpha")
        with self.assertRaisesRegex(Held, "alpha.*trial row missing source_sha256"):
            match.submit(self.project, self.policy, source)
        self.draft("alpha")
        source.write_text("int alpha(void) { return 2; }\n")
        with self.assertRaisesRegex(Held, "alpha.*trial row missing source_sha256"):
            match.submit(self.project, self.policy, source)
        self.assertFalse((self.root / "build" / "match").exists())

    def test_latest_trial_for_identity_overrides_older_success(self) -> None:
        source = self.draft("alpha")
        self.prove(source, identical=False)
        with self.assertRaisesRegex(Held, "alpha.*identical_everywhere"):
            match.submit(self.project, self.policy, source)
        self.prove(source)
        match.submit(self.project, self.policy, source)

    def test_trial_on_published_wrapper_has_same_submit_identity(self) -> None:
        source = self.sources / "alpha.c"
        plain = b"#if DEBUG\nint alpha(void) { return 0; }\n#endif\n"
        source.write_bytes(b"#ifdef NON_MATCHING\n" + plain + b"#endif\n")
        from unbake.decomp.trial import Trial
        from unbake.decomp.trial_compare import TYPES, Compare

        result = Trial(
            "alpha",
            drafts.source_identity(source.read_bytes()),
            {v: Compare(v, 4, 4, dict.fromkeys(TYPES, 0), [], 100, ()) for v in self.versions},
            [],
            "match submit alpha.c",
        )
        self.store.add(result, source, {v: 100 for v in self.versions})
        record = self.store.rows("alpha")[-1]
        self.assertNotEqual(record["sha256"], record["source_sha256"])
        match.submit(self.project, self.policy, source)
        self.assertEqual(self.queued()[0]["source_sha256"], record["source_sha256"])
        self.assertEqual(Path(self.queued()[0]["source"]).read_bytes(), plain)
        source.write_bytes(b"#ifdef NON_MATCHING\n" + plain.replace(b"return 0", b"return 1") + b"#endif\n")
        with self.assertRaisesRegex(Held, "alpha.*trial row missing source_sha256"):
            match.submit(self.project, self.policy, source)

    def test_submit_requires_proof_for_every_version(self) -> None:
        source = self.draft("alpha", versions=["us"])
        with self.assertRaisesRegex(Held, "alpha.*compares.*eu"):
            match.submit(self.project, self.policy, source)

    def test_inline_asm_is_refused_but_comments_and_strings_are_allowed(self) -> None:
        for keyword in ("asm", "__asm", "__asm__", "#pragma asm"):
            with self.subTest(keyword=keyword):
                source = self.draft("alpha", f'{keyword}("nop");\nint alpha(void) {{ return 0; }}\n')
                with self.assertRaisesRegex(Held, "alpha.*inline-asm"):
                    match.submit(self.project, self.policy, source)
        source = self.draft(
            "alpha",
            '/* __asm__("nop") */\nint alpha(void) {\n  // asm nop\n  const char *label = "__asm__"; return 0;\n}\n',
        )
        match.submit(self.project, self.policy, source)
        self.assertEqual(len(self.queued()), 1)

    def test_identical_proof_requires_unmarked_checks_to_pass(self) -> None:
        for content, rule in [
            ("volatile int alpha;", "volatile-storage"),
            ("int alpha(void) { return *(int*)((char*)p + 20); }", "raw-offset"),
        ]:
            with self.subTest(rule=rule):
                source = self.draft("alpha", content)
                with self.assertRaisesRegex(Held, rule):
                    match.submit(self.project, self.policy, source, function_name="alpha")
        self.assertEqual(self.calls, [])

    def test_guarded_submission_requires_exact_proof_then_strips_only_wrapper(self) -> None:
        for function, guarded in [("alpha", False), ("beta", True)]:
            with self.subTest(guarded=guarded):
                source = self.draft(function, f"#if DEBUG\nint {function}(void) {{ return 0; }}\n#endif\n")
                original = source.read_text()
                published = self.src / source.name
                published.write_text("#ifdef NON_MATCHING\n" + original + "#endif\n")
                submitted = published if guarded else source
                match.submit(self.project, self.policy, submitted)
                proof_sha = hashlib.sha256(original.encode()).hexdigest()
                receipts = match.run(self.project, self.policy)
                self.assertTrue(any(f"{function} matched" in line for line in receipts), receipts)
                self.assertEqual(published.read_text(), original)
                row = next(row for row in self.matched() if row["function"] == function)
                self.assertEqual(row["sha256"], proof_sha)
        self.assertFalse((self.root / "data" / "matched.jsonl").exists())

    def test_marked_findings_resolve_and_reasons_survive_host_receipt(self) -> None:
        reason = "preserve the measured scheduling effect."
        content = f"/* FAKEMATCH: {reason} */\nint alpha(void) {{ do {{}} while (0); return 0; }}\n"
        finding = needs.GuardFinding("empty-loop", 2, content.splitlines()[1], reason)
        source = self.draft("alpha", content, pending=[finding])
        match.submit(self.project, self.policy, source)
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("alpha matched" in line for line in receipts), receipts)
        self.assertEqual(self.matched()[0]["fakematch"], [reason])
        self.assertFalse((self.root / "data" / "matched.jsonl").exists())

    def test_absent_version_is_allowed_and_duplicate_function_is_refused(self) -> None:
        path = self.project.version("eu").split
        original = path.read_text()
        path.write_text(original.replace("text/alpha", "missing"))
        source = self.draft("alpha")
        match.submit(self.project, self.policy, source)
        self.assertEqual(match.holding_versions(self.project, "alpha"), ("us",))
        path.write_text(original.replace("      - [0x1010, asm, beta]", "      - [0x1010, asm, alpha]"))
        with self.assertRaisesRegex(Held, "alpha.*eu.*found 2"):
            match.submit(self.project, self.policy, self.draft("alpha"))
        self.assertEqual(self.calls, [])

    def test_static_run_refusal_happens_before_any_stage_or_build(self) -> None:
        self.queue("alpha")
        source = self.sources / "alpha.c"
        source.write_text("changed draft\n")
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("alpha" in line and "source_sha256" in line for line in receipts))
        self.assertFalse((self.root / "build" / "match").exists())
        self.assertEqual(self.calls, [])
        self.assert_untouched()
        self.assertEqual(len(self.queued()), 1)
