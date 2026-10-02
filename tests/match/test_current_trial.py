"""Current input checks and atomic overlay publication."""

from dataclasses import replace
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.decomp import work
from unbake.match import nonmatching, proof, queue
from unbake.project.config import Held


class CurrentTrialTests(MatchFixture):
    def setUp(self) -> None:
        super().setUp()
        for version in self.versions:
            for name in ("text/alpha", "beta", "gamma"):
                path = self.original[version] / "obj/asm" / (name + ".o")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pinned target")

    def test_changed_untried_bytes_refuse_before_queue_or_publication(self) -> None:
        source = self.draft("alpha")
        source.write_text(source.read_text() + "/* edited */\n")
        with self.assertRaisesRegex(Held, "trial.source_sha256"):
            queue.publish_source(self.project, self.policy, source)
        self.assertEqual(self.queued(), [])
        self.assert_untouched()

    def test_header_compiler_layout_and_generation_changes_refuse(self) -> None:
        source = self.draft("alpha", '#include "types.h"\nint alpha(void) { return 0; }\n')
        header = self.project.include[0] / "types.h"
        original = header.read_bytes()
        header.write_bytes(original + b"/* header edit */\n")
        with self.assertRaisesRegex(Held, "submit.overlay_sha256"):
            proof.ensure(self.project, self.policy, source, self.versions)
        header.write_bytes(original)
        changed = replace(
            self.project, compilers={ident: replace(c, cflags=("-O0",)) for ident, c in self.project.compilers.items()}
        )
        with self.assertRaisesRegex(Held, "submit.flags"):
            proof.ensure(changed, self.policy, source, self.versions)
        target = self.original["us"] / "obj/asm/text/alpha.o"
        target.write_bytes(b"changed target")
        with self.assertRaisesRegex(Held, "submit.target_sha256"):
            proof.ensure(self.project, self.policy, source, self.versions)

    def test_unrelated_headers_layout_and_config_evidence_keep_receipt_valid(self) -> None:
        source = self.draft("alpha")
        (self.project.include[0] / "unrelated.h").write_text("typedef int Other;\n")
        with (self.root / "config.toml").open("a") as output:
            output.write('[compiler_selections.other]\nevidence_json="{}"\n')
        for version in self.versions:
            symbols = self.project.version(version).symbols
            symbols.write_text(symbols.read_text().replace("beta = 0x80001010", "beta = 0x80001014"))
        proof.ensure(self.project, self.policy, source, self.versions)

    def test_forced_header_and_effective_assembler_flags_invalidate_receipt(self) -> None:
        path = self.root / "config.toml"
        with path.open("a") as output:
            output.write('[build.unit_cflags]\nalpha=["-include", "types.h"]\n')
        source = self.draft("alpha")
        header = self.project.include[0] / "types.h"
        before = header.read_bytes()
        header.write_bytes(before + b"/* changed forced input */\n")
        with self.assertRaisesRegex(Held, "submit.overlay_sha256"):
            proof.ensure(self.project, self.policy, source, self.versions)
        header.write_bytes(before)
        path.write_text(path.read_text().replace("sn64_asflags=[]", 'sn64_asflags=["-G0"]'))
        with self.assertRaisesRegex(Held, "submit.compiler_sha256"):
            proof.ensure(self.project, self.policy, source, self.versions)

    def test_partial_or_old_exact_trial_cannot_authorize_submission(self) -> None:
        source = self.draft("alpha", versions=("us",))
        with self.assertRaisesRegex(Held, "submit.versions"):
            queue.publish_source(self.project, self.policy, source)
        self.prove(source)
        self.prove(source, identical=False)
        with self.assertRaisesRegex(Held, "submit.owner_fuzzy_bar"):
            queue.publish_source(self.project, self.policy, source)

    def test_workspace_store_is_isolated(self) -> None:
        source = self.draft("alpha")
        changed = replace(self.project, workspace_id="00000000-0000-4000-8000-000000000003")
        with self.assertRaisesRegex(Held, "trial.source_sha256"):
            proof.ensure(changed, self.policy, source, self.versions)

    def test_changed_private_provider_evidence_requires_another_trial(self) -> None:
        manifest = self.project.root / "docs/setup/us.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"providers": []}\n')
        source = self.draft("alpha")
        manifest.write_text('{"providers": [{"kind": "private", "owners": ["alpha"]}]}\n')
        with self.assertRaisesRegex(Held, "submit.layout_sha256"):
            proof.ensure(self.project, self.policy, source, self.versions)
        self.assertEqual(self.queued(), [])
        self.assert_untouched()

    def test_changed_unreferenced_entry_invalidates_exact_and_fuzzy_receipts(self) -> None:
        source = self.draft("alpha")
        for version in self.versions:
            symbols = self.project.version(version).symbols
            symbols.write_text(symbols.read_text() + "secondary = 0x80001008; // type:func\n")
        for admit in (
            lambda: proof.ensure(self.project, self.policy, source, self.versions),
            lambda: nonmatching.admit(self.project, self.policy, source),
        ):
            with self.assertRaisesRegex(Held, "submit.entries"):
                admit()
        self.assertEqual(self.queued(), [])
        self.assert_untouched()

    def test_type_feedback_failure_preserves_truthful_publication_receipts(self) -> None:
        source = self.draft("alpha")
        with patch("unbake.decomp.type_context.feedback", side_effect=Held("types", "types.conflict: named conflict")):
            lines = queue.publish_source(self.project, self.policy, source)
        self.assertTrue(any(line.startswith("OK(match): alpha matched") for line in lines), lines)
        self.assertEqual(sum(line.startswith("OK(submit):") for line in lines), len(self.versions))
        self.assertTrue(
            any(line.startswith("HELD(types): types.conflict:") and "was published" in line for line in lines), lines
        )
        self.assertTrue((self.project.src / "alpha.c").is_file())
        self.assertEqual(self.queued(), [])

    def test_overlay_is_published_only_with_successful_rom_proof(self) -> None:
        directory = self.project.drafts / "alpha"
        directory.mkdir(parents=True)
        source = directory / "alpha.c"
        source.write_text("int alpha(void) { return 0; }\n")
        staged = work.overlay(self.project, directory)
        header = staged.include[0] / "new.h"
        header.write_text("typedef int OverlayType;\n")
        work.save_overlay(self.project, directory)
        self.prove(source)
        self.build_failures.add(("alpha", "eu"))
        lines = queue.publish_source(self.project, self.policy, source)
        self.assertTrue(any("submit.sha1.eu" in line for line in lines))
        self.assertFalse((self.project.include[0] / "new.h").exists())
        self.assert_untouched()
        self.build_failures.clear()
        lines = queue.publish_source(self.project, self.policy, source)
        self.assertTrue(any("matched" in line for line in lines), lines)
        self.assertEqual((self.project.include[0] / "new.h").read_text(), header.read_text())
        self.assertTrue((self.project.src / "alpha.c").is_file())
        self.assertEqual(len(self.matched()), 1)
