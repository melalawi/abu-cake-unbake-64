"""Actual 12670 provider rewrite must retire its old score, preserving history."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config
from unbake.config import Held
from unbake.project import publication_push
from unbake.report import progress, state, verify
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/ragewars_retained_12670"
NAME = "func_80212670_de"
OLD = (FIXTURE / "qualified.c").read_bytes()
CURRENT = (FIXTURE / "retained.c").read_bytes()
EVENT = json.loads((FIXTURE / "publication.json").read_text())
RECEIPT = EVENT["result"]["value"]["publication"]["receipt"]
PROVIDER = "func_8020D1CC_de"


class RetainedSourceStateTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def setUp(self):
        super().setUp()
        for path in [
            self.project.root / "layout.toml",
            *self.project.root.glob("versions/*/*.yaml"),
            *self.project.root.glob("versions/*/symbol_addrs.txt"),
        ]:
            path.write_text(path.read_text().replace("beta", NAME))
        # Keep the real immutable event identity and payload.
        cfg = self.project.root / "config.toml"
        cfg.write_text(
            cfg.read_text().replace(self.project.id, EVENT["project_id"]).replace("ido-7.1", RECEIPT["compiler"])
        )
        (self.project.tools / "ido-7.1").rename(self.project.tools / RECEIPT["compiler"])
        self.project = config.load(self.project.root)
        self.source = self.project.src / (NAME + ".c")
        self.source.write_bytes(CURRENT)
        self.history = attempts.encoded(EVENT) + b"\n"
        (self.project.root / attempts.PATH).write_bytes(self.history)
        header = self.project.include[-1] / "shared/route_relation_grid.h"
        header.parent.mkdir(parents=True)
        header.write_bytes((FIXTURE / "route_relation_grid.h").read_bytes())

    def native_provider(self):
        self.provider = self.project.src / (PROVIDER + ".c")
        self.provider.write_bytes((FIXTURE / "provider.c").read_bytes())
        holders = json.loads((FIXTURE / "holders.json").read_text())
        providers = json.loads((FIXTURE / "provider-holders.json").read_text())
        for draft_row, provider in zip(holders, providers, strict=True):
            version = draft_row["version"]
            raw = bytes.fromhex(provider["words"])
            draft_raw = bytes.fromhex(draft_row["words"])
            meta = self.project.version(version)
            meta.baserom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + raw + draft_raw)
            meta.split.write_text(
                "name: fixture\nsegments:\n  - [0x0, header, header]\n"
                f"  - name: main\n    type: code\n    start: 0x40\n    vram: 0x{provider['address']:X}\n"
                f"    subalign: 4\n    subsegments:\n      - [0x40, c, {PROVIDER}]\n"
                f"  - name: draft\n    type: code\n    start: 0x{0x40 + len(raw):X}\n"
                f"    vram: 0x{draft_row['address']:X}\n    subalign: 4\n    subsegments:\n"
                f"      - [0x{0x40 + len(raw):X}, asm, {NAME}]\n  - [0x{0x40 + len(raw) + len(draft_raw):X}]\n"
            )
            meta.symbols.write_text(f"{PROVIDER} = 0x{provider['address']:X};\n{NAME} = 0x{draft_row['address']:X};\n")
            native = self.project.build_link(version) / "units" / (PROVIDER + ".bin")
            native.parent.mkdir(parents=True, exist_ok=True)
            # Model the external native output using the five actual ROM extents;
            # this unit fixture does not claim a fresh compiler qualification.
            native.write_bytes(raw)

    def git(self, project, *args):
        if args[:2] == ("diff", "--name-only"):
            return "\0".join((f"src/{NAME}.c", f"src/{PROVIDER}.c"))
        return ""

    def generate(self):
        buildfiles.write_progress(self.project, publish_branch="main")
        with patch.object(progress, "readme_descriptions", return_value={v: f"{v} (fixture)" for v in self.versions}):
            progress.write(self.project, self.host)
        return verify.validate(self.project)

    def test_actual_12670_final_guarded_source_has_unknown_similarity_and_immutable_history(self):
        self.assertEqual(hashlib.sha256(OLD).hexdigest(), RECEIPT["source_sha256"])
        current = state.inventory(self.project)
        active = current.receipts[NAME]
        self.assertEqual(active["source_sha256"], hashlib.sha256(CURRENT).hexdigest())
        self.assertIsNone(active["score"])
        self.assertEqual(active["versions"], {v: None for v in self.versions})
        manifest = self.generate()
        for version in self.versions:
            draft = manifest["versions"][version]["drafts"][0]
            self.assertEqual(draft["source_sha256"], active["source_sha256"])
            self.assertIsNone(draft["score"])
            self.assertEqual(manifest["versions"][version]["draft_weighted_bytes"], 0)
        self.assertEqual((self.project.root / attempts.PATH).read_bytes(), self.history)
        self.assertEqual(attempts.ledger(self.project).summaries()[NAME].fuzzy, RECEIPT)
        self.assertIn("-DNON_MATCHING", buildfiles.units_mk(self.project))

    def test_actual_old_qualification_remains_scored_only_for_the_qualified_source(self):
        self.source.write_bytes(OLD)
        self.assertEqual(state.inventory(self.project).receipts[NAME], RECEIPT)
        manifest = self.generate()
        for version in self.versions:
            self.assertEqual(manifest["versions"][version]["drafts"][0]["score"], 82.653061)
        self.assertEqual((self.project.root / attempts.PATH).read_bytes(), self.history)

    def test_actual_stale_claim_is_rejected_and_unverified_edits_receive_no_old_score(self):
        with self.assertRaises(Held) as caught:
            state.inventory(self.project, receipts={NAME: RECEIPT})
        self.assertEqual(caught.exception.key, "source.hash")
        self.generate()
        self.source.write_bytes(CURRENT + b"\n/* unqualified edit */\n")
        self.assertIsNone(state.inventory(self.project).receipts[NAME]["score"])
        with self.assertRaises(Held):
            verify.validate(self.project)
        self.generate()
        self.assertEqual((self.project.root / attempts.PATH).read_bytes(), self.history)

    def test_guarded_source_without_qualification_is_retained_and_not_credited(self):
        (self.project.root / attempts.PATH).write_bytes(b"")
        active = state.inventory(self.project).receipts[NAME]
        self.assertEqual(active["versions"], {v: None for v in self.versions})
        self.generate()
        self.assertIn("-DNON_MATCHING", buildfiles.units_mk(self.project))

    def test_actual_provider_native_admission_does_not_credit_guarded_12670_and_remains_strict(self):
        self.native_provider()
        with patch.object(publication_push, "_git", side_effect=self.git):
            result = publication_push.admission(self.project, self.host, "a" * 40, "b" * 40)
        self.assertEqual(result["work"]["functions_compared"], 5)
        self.assertEqual(result["work"]["native_bytes_read"], 240)
        self.assertEqual({r["function"] for r in result["scopes"]}, {PROVIDER})
        manifest = self.generate()
        for version in self.versions:
            self.assertIsNone(manifest["versions"][version]["drafts"][0]["score"])
        native = self.project.build_link("us-rev1") / "units" / (PROVIDER + ".bin")
        raw = native.read_bytes()
        native.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        with patch.object(publication_push, "_git", side_effect=self.git), self.assertRaises(Held) as caught:
            publication_push.admission(self.project, self.host, "a" * 40, "b" * 40)
        self.assertEqual(caught.exception.key, "publish.native_mismatch")
        native.write_bytes(raw)
        self.source.write_bytes(CURRENT + b'\nvoid bad(void) { __asm__("nop"); }\n')
        with patch.object(publication_push, "_git", side_effect=self.git), self.assertRaises(Held) as caught:
            publication_push.admission(self.project, self.host, "a" * 40, "b" * 40)
        self.assertEqual(caught.exception.key, "publish.push_rules")

    def test_claimed_current_receipt_compiler_membership_and_exact_source_drift_are_rejected(self):
        active = state.inventory(self.project).receipts[NAME]
        for field, value, key in [
            ("compiler", "wrong", "source.compiler"),
            ("versions", {"us-rev1": None}, "source.versions"),
        ]:
            with self.subTest(field=field), self.assertRaises(Held) as caught:
                state.inventory(self.project, receipts={NAME: {**active, field: value}})
            self.assertEqual(caught.exception.key, key)
        self.native_provider()
        self.generate()
        self.provider.write_bytes(self.provider.read_bytes() + b"\n/* changed exact source */\n")
        with self.assertRaises(Held) as caught:
            verify.validate(self.project)
        self.assertEqual(caught.exception.key, "report.state")
