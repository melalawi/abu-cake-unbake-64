"""Installed CLI batches attribute simultaneous byte faults and retain learned results."""

import hashlib
import json
import os
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import toml

from tests.cli import test_publication_boundary as fixture
from unbake.match import batch, incremental
from unbake.project import config, makefile


class BatchPublicationCliTests(unittest.TestCase):
    names = tuple(f"item_{index:02d}" for index in range(32))

    def setUp(self):
        fixture.PublicationBoundaryCliTests.setUp(self)
        path = self.root / "config.toml"
        data = toml.loads(path.read_text())
        for version in self.project.versions:
            data["version"][version].update(cartridge_id="NUS-TEST-0", region="USA", description="Synthetic release.")
        path.write_text(toml.dumps(data))
        self.project = config.load(self.root)

    write_policy = fixture.PublicationBoundaryCliTests.write_policy
    cli = fixture.PublicationBoundaryCliTests.cli
    make = fixture.PublicationBoundaryCliTests.make

    run_cli = fixture.PublicationBoundaryCliTests.run_cli

    def planter(self, *, block=False):
        bad = {self.names[5], self.names[17], self.names[29]}
        self.tools.bad = bad
        marker = self.directory / "subset-link"
        if block:

            def stop(project, names):
                if len(names) < len(self.names):
                    marker.touch()
                    refusals = [row["line"] for row in self.evidence() if row["event"] == "receipt"]
                    for name in bad:
                        self.assertTrue(any(f"HELD(match): {name}:" in line for line in refusals))
                    raise KeyboardInterrupt("stop subset proof")

            self.tools.proof_hook = stop
        return bad, marker

    def evidence(self):
        return [
            json.loads(line)
            for path in (self.root / ".unbake/state/publications").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]

    def test_declaration_refusal_precedes_rom_proof_and_publication(self):
        source = self.sources[0]
        generations = {v: self.project.build_link(v).resolve() for v in self.project.versions}
        with (
            patch("unbake.typemap.declarations.validate_sources", return_value={source.stem: "types.declaration: bad"}),
            patch.object(incremental, "prepare") as proof,
            patch.object(batch, "_commit") as commit,
        ):
            result = self.run_cli(
                [str(self.script), "--project", str(self.root), "submit", "--batch", str(source)],
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("types.declaration: bad", result.stdout + result.stderr)
        proof.assert_not_called()
        commit.assert_not_called()
        self.assertFalse((self.project.src / source.name).exists())
        self.assertEqual(generations, {v: self.project.build_link(v).resolve() for v in self.project.versions})

    def test_second_batch_reuses_published_objects_and_links_once(self):
        self.cli("submit", self.sources[0])
        generations = {v: (self.project.build / v).resolve() for v in self.project.versions}
        retained = {v: (g / "obj/src" / (self.names[0] + ".o")).read_bytes() for v, g in generations.items()}
        # An unrelated new header must not invalidate the previously published unit.
        (self.project.include[0] / "unused.h").write_text("typedef int Unused;\n")
        self.cli("submit", "--batch", *self.sources[1:])
        compiled = [row for row in self.evidence() if row["event"] == "compile"]
        self.assertEqual(len(compiled), 2 * len(self.project.versions))
        later = [row for row in compiled if len(row["sources"]) > 1]
        self.assertEqual(len(later), len(self.project.versions))
        for row in later:
            self.assertEqual(set(row["sources"]), set(self.names[1:]))
        proofs = [row for row in self.evidence() if row["event"] == "proof"]
        self.assertEqual(len(proofs), 2)
        self.assertTrue(all(not row["failures"] for row in proofs))
        for version, content in retained.items():
            generation = (self.project.build / version).resolve()
            self.assertEqual((generation / "obj/src" / (self.names[0] + ".o")).read_bytes(), content)
            self.assertFalse((generation / "obj/asm").is_symlink())
            self.assertTrue((generation / "obj/asm").is_dir())
        self.assertIn(": OK", self.make())

    def test_feedback_header_writes_precede_receipt_refresh_for_new_and_edited_sources(self):
        events = []
        original = batch.staging.publication_stamps
        header = self.project.include[0] / "shared/typemap.h"

        def feedback(project, policy, candidates, current, *, strict=False):
            events.append(("feedback", strict))
            header.write_bytes(header.read_bytes() + b"\n/* feedback wrote header */\n")
            return ["OK(types): mocked; follow-up: mock"]

        def refresh(project, generations):
            events.append(("refresh", None))
            original(project, generations)
            for generation in generations.values():
                for stamp in (generation / "obj").rglob("*.built"):
                    self.assertGreaterEqual(stamp.stat().st_mtime_ns, header.stat().st_mtime_ns)

        with (
            patch.object(batch, "_feedback", side_effect=feedback),
            patch.object(batch.staging, "publication_stamps", side_effect=refresh),
        ):
            self.assertIn("OK(types): mocked", self.cli("submit", self.sources[0]))
            published = self.project.src / self.sources[0].name
            published.write_bytes(published.read_bytes() + b"\n/* republication */\n")
            self.cli("submit", published)
        self.assertEqual(events, [("feedback", False), ("refresh", None), ("feedback", True), ("refresh", None)])

    def test_refresh_failure_after_feedback_restores_headers_and_type_receipts(self):
        before = fixture.PublicationBoundaryCliTests.inputs(self)
        type_paths = [
            self.project.build / "types" / name
            for name in ("proven.json", "database.json", "summary.json", "redraft.json")
        ]
        snapshots = {path: path.read_bytes() if path.is_file() else None for path in type_paths}
        generations = {v: self.project.build_link(v).resolve() for v in self.project.versions}

        def feedback(project, *args, **kwargs):
            (project.include[0] / "shared/typemap.h").write_text("changed after feedback")
            for path in type_paths:
                path.write_text("changed after feedback")
            return []

        with (
            patch.object(batch, "_feedback", side_effect=feedback),
            patch.object(batch.staging, "publication_stamps", side_effect=OSError("refresh failed")),
            self.assertRaisesRegex(OSError, "refresh failed"),
        ):
            batch.publish(self.project, self.policy, self.sources[:1])
        self.assertEqual(fixture.PublicationBoundaryCliTests.inputs(self), before)
        self.assertEqual({v: self.project.build_link(v).resolve() for v in self.project.versions}, generations)
        self.assertEqual({path: path.read_bytes() if path.is_file() else None for path in type_paths}, snapshots)

    def test_receipt_refresh_failure_rolls_back_inputs_and_generations(self):
        before = fixture.PublicationBoundaryCliTests.inputs(self)
        generations = {v: self.project.build_link(v).resolve() for v in self.project.versions}
        with (
            patch.object(batch.staging, "publication_stamps", side_effect=OSError("receipt refresh failed")),
            self.assertRaisesRegex(OSError, "receipt refresh failed"),
        ):
            batch.publish(self.project, self.policy, self.sources[:2])
        self.assertEqual(fixture.PublicationBoundaryCliTests.inputs(self), before)
        self.assertEqual({v: self.project.build_link(v).resolve() for v in self.project.versions}, generations)

    def test_changed_header_cannot_reuse_a_now_mismatching_published_object(self):
        header = self.project.include[0] / "value.h"
        header.write_text("#define PROOF_VALUE 1\n")
        first = self.sources[0]
        first.write_text(f'#include "value.h"\nint {first.stem}(void) {{ return PROOF_VALUE; }}\n')
        self.cli("submit", first)
        generations = {v: (self.project.build / v).resolve() for v in self.project.versions}
        header.write_text("#define PROOF_VALUE 2\n")
        result = self.run_cli(
            [str(self.script), "--project", str(self.root), "submit", str(self.sources[1])],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("submit.dependencies", output)
        self.assertIn(self.names[0], output)
        self.assertFalse((self.project.src / self.sources[1].name).exists())
        self.assertEqual(generations, {v: (self.project.build / v).resolve() for v in self.project.versions})

    def test_three_changed_sources_are_isolated_and_29_publish(self):
        for source in self.sources:
            self.cli("try", source)
        bad = {self.names[5], self.names[17], self.names[29]}
        for source in self.sources:
            if source.stem in bad:
                source.write_text(source.read_text().replace("return 1", "return 2"))
        result = self.run_cli(
            [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for name in self.names:
            if name in bad:
                # A source edited after its try is proved like any other and refused by its bytes.
                self.assertIn(f"HELD(match): {name}: build compare failed", result.stdout)
                self.assertFalse((self.project.src / (name + ".c")).exists())
            else:
                self.assertIn(f"{name} matched on VERSION", result.stdout)
        proofs = [row for row in self.evidence() if row["event"] == "proof"]
        # Object preflight names byte faults before the passing subset links once.
        self.assertEqual(len(proofs), 1)
        self.assertEqual(len(proofs[-1]["sources"]), 32)
        attribution = [row for row in self.evidence() if row["event"] == "attribution"]
        self.assertEqual(set(attribution[-1]["culprits"]), bad)
        self.assertFalse(proofs[-1]["failures"])
        self.assertIn(": OK", self.make())
        self.assertFalse(list(self.project.build.glob("submit-*")))

    def test_three_bad_objects_in_32_publish_29_in_two_proofs(self):
        # Projects retain generated helpers from setup. A submit must stage
        # current drivers and their checksums before its cartridge proof.
        checksum = self.project.tools / "compiler.sha256"
        checksum_text = checksum.read_text()
        for filename in ("compile.py", "pool_slices.py"):
            helper = self.project.tools / filename
            old_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            helper.write_text("raise RuntimeError('obsolete generated helper')\n" + helper.read_text())
            new_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            checksum_text = checksum_text.replace(old_digest, new_digest)
        checksum.write_text(checksum_text)
        for source in self.sources:
            self.cli("try", source)
        bad, _ = self.planter()
        result = self.run_cli(
            [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for name in self.names:
            if name in bad:
                self.assertIn(f"HELD(match): {name}: build compare failed", result.stdout)
                self.assertIn(f"object obj/src/{name}.o .text", result.stdout)
                self.assertIn(f"symbol {name}", result.stdout)
                self.assertFalse((self.project.src / (name + ".c")).exists())
            else:
                self.assertIn(f"{name} matched on VERSION", result.stdout)
                self.assertTrue((self.project.src / (name + ".c")).exists())
        proofs = [row for row in self.evidence() if row["event"] == "proof"]
        self.assertEqual(len(proofs), 2, proofs)
        self.assertEqual(len(proofs[0]["sources"]), 32)
        self.assertEqual(len(proofs[1]["sources"]), 29)
        self.assertFalse(proofs[1]["failures"])
        expected_helpers = makefile.helpers(config.load(self.root))
        for filename in ("compile.py", "pool_slices.py"):
            helper = self.project.tools / filename
            self.assertEqual(helper.read_text(), expected_helpers["tools/" + filename])
            self.assertIn(hashlib.sha256(helper.read_bytes()).hexdigest(), checksum.read_text())
        self.assertNotIn("compiler_selections", toml.loads((self.root / "config.toml").read_text()))
        self.assertIn(": OK", self.make())

    def test_stopped_subset_proof_keeps_named_refusals_on_disk_and_stdout(self):
        for source in self.sources:
            self.cli("try", source)
        bad, marker = self.planter(block=True)
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from unbake.cli.main import main

        stream = io.StringIO()
        with patch.dict(os.environ, self.env), redirect_stdout(stream):
            status = main(["--project", str(self.root), "submit", "--batch", *map(str, self.sources)])
        self.assertEqual(status, 130, stream.getvalue())
        self.assertIn("interrupted", stream.getvalue())
        self.assertTrue(marker.exists())
        for name in bad:
            self.assertIn(f"HELD(match): {name}:", stream.getvalue())
        self.assertEqual(len([row for row in self.evidence() if row["event"] == "proof"]), 1)
        self.assertFalse(list(self.project.src.glob("*.c")))
        self.assertFalse(list(self.project.build.glob("submit-*")))

    def test_preflight_compile_hold_and_proof_faults_preserve_survivors(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                if fallback:
                    path = self.root / "config.toml"
                    data = toml.loads(path.read_text())
                    data["build"]["cppflags"].append("-DPROOF_BATCH=1")
                    path.write_text(toml.dumps(data))
                self.sources[5].write_text(f"int {self.names[5]}(void) {{ invalid C; }}\n")
                self.sources[17].write_text(
                    f"extern int missing(void); int {self.names[17]}(void) {{ return missing(); }}\n"
                )
                self.sources[29].write_text(f"int {self.names[29]}(void) {{ return 2; }}\n")
                # A known compile fault is held before either proof path.
                sources = [self.sources[i] for i in (5, 17, 29)] if fallback else self.sources
                before = {json.dumps(row, sort_keys=True) for row in self.evidence()}
                built = len(self.tools.built)
                # Make is a fake here: force its fallback boundary while retaining
                # the real attribution and survivor relink decisions.
                context = patch.object(incremental, "prepare", return_value=None) if fallback else nullcontext()
                with context:
                    result = self.run_cli(
                        [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, sources)],
                        env=self.env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                    )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                rows = [row for row in self.evidence() if json.dumps(row, sort_keys=True) not in before]
                self.assertIn(f"HELD(types): {self.names[5]}: VERSION", result.stdout)
                self.assertIn("invalid C", result.stdout)
                self.assertEqual(len(self.tools.built) - built, int(fallback))
                proofs = [row for row in rows if row["event"] == "proof"]
                self.assertEqual(len(proofs), 1, result.stdout + result.stderr)
                self.assertEqual(bool([row for row in rows if row["event"] == "compile"]), not fallback)
                attribution = [row for row in rows if row["event"] == "attribution"]
                culprits = attribution[0]["culprits"]
                self.assertEqual(set(culprits), {self.names[i] for i in (17, 29)})
                self.assertTrue(
                    any(
                        "undefined reference to" in reason and "missing" in reason
                        for reason in culprits[self.names[17]]
                    )
                )
                self.assertIn("produced", "; ".join(culprits[self.names[29]]))
                self.assertIn(": OK", self.make())

    def test_late_candidate_compile_failure_is_held_and_other_sources_publish(self):
        self.sources[5].write_text(f"int {self.names[5]}(void) {{ invalid C; }}\n")
        with patch.object(
            batch, "_type_preflight", side_effect=lambda project, policy, candidates, receipts: candidates
        ):
            result = self.run_cli(
                [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"HELD(match): {self.names[5]}:", result.stdout)
        self.assertIn("compile diagnostic", result.stdout)
        self.assertNotIn("submit.dependencies", result.stdout)
        self.assertFalse((self.project.src / self.sources[5].name).exists())
        for index, source in enumerate(self.sources):
            if index != 5:
                self.assertIn(f"{source.stem} matched on VERSION", result.stdout)
                self.assertTrue((self.project.src / source.name).is_file())
        self.assertIn(": OK", self.make())

    def test_folded_port_and_next_batch_reuse_every_extraction(self):
        first = self.sources[0]
        first.write_text(f"int {self.names[0]}(void) {{ return 1; }}\nint {self.names[1]}(void) {{ return 1; }}\n")
        self.cli("submit", first)
        self.cli("submit", "--batch", *self.sources[2:])
        rows = self.evidence()
        self.assertFalse([row for row in rows if row["event"] == "incremental_fallback"])
        relinks = [row for row in rows if row["event"] == "relink"]
        self.assertEqual(len(relinks), 2)
        self.assertTrue(all(all(value == "True" for value in row["reused"].values()) for row in relinks))
        proofs = [row for row in rows if row["event"] == "proof"]
        self.assertEqual(len(proofs), 2)
        self.assertTrue(all(not row["failures"] for row in proofs))
        self.assertIn(": OK", self.make())
