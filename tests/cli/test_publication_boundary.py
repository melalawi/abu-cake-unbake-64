"""Publication transactions with canned tool results and overlapping threads."""

import fcntl
import hashlib
import json
import os
import shutil
import sysconfig
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import toml

from tests.support import test_policy
from unbake.project import config, setup
from unbake.report import progress


class PublicationBoundaryCliTests(unittest.TestCase):
    def setUp(self):
        from tests.process_fakes import compiler_registry

        compiler_registry(self)
        names = getattr(self, "names", ("alpha", "beta"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "project"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.root)
        shutil.rmtree(self.root / "src")
        (self.root / "src").mkdir()
        data = toml.loads((self.root / "config.toml").read_text())
        data["project"].update(default_compiler="ido-7.1")
        data["build"]["asflags"] = ["-EB", "-mips3", "-G0", "--no-pad-sections"]
        for key, field in (
            ("as", "mips_as"),
            ("ld", "mips_ld"),
            ("objcopy", "mips_objcopy"),
            ("splat", "splat"),
            ("cpp", "cpp"),
        ):
            data["build"][key] = "policy:" + field
        data["compilers"]["ido-5.3"] = dict(data["compilers"]["ido-7.1"])
        data["units"] = {name: "ido-5.3" for name in names}
        body = bytes.fromhex("03e0000824020001") * len(names)
        image = bytes.fromhex("80371240") + bytes(60) + body
        for version in data["project"]["versions"]:
            cartridge = data["version"][version]
            cartridge.update(cartridge_id="FIX-" + version, region="Test", description="Synthetic release.")
            path = self.root / cartridge["baserom"]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(image)
            cartridge["baserom_sha1"] = hashlib.sha1(image).hexdigest()
            path = self.root / cartridge["split"]
            text = path.read_text().split("    subsegments:", 1)[0]
            text = text.replace("[0x0, header, header]", "[0x0, bin, header]")
            text += (
                "    subsegments:\n"
                + "".join(f"      - [0x{0x40 + index * 8:X}, asm, {name}]\n" for index, name in enumerate(names))
                + f"  - [0x{0x40 + len(names) * 8:X}]\n"
            )
            path.write_text(text)
            (self.root / cartridge["symbols"]).write_text(
                "".join(f"{name} = 0x{0x80001000 + index * 8:08X};\n" for index, name in enumerate(names))
            )
        (self.root / "config.toml").write_text(toml.dumps(data))
        self.project = config.load(self.root)
        from unbake.layout import map as ownership

        def synthetic_map(selected):
            members = ownership.catalog(selected)
            return ownership.Map(
                2, tuple(ownership.Group(m.name, m.segment, "default", (m.name,)) for m in members.values())
            )

        mapping = patch.object(ownership, "load", side_effect=synthetic_map)
        mapping.start()
        self.addCleanup(mapping.stop)

        reports = {
            version: {
                "version": 2,
                "measures": {
                    "complete_code": 0,
                    "total_code": 8 * len(names),
                    "complete_units": 0,
                    "total_units": len(names),
                },
            }
            for version in self.project.versions
        }
        document = (
            "# Fixture\n\n## Progress\n\n"
            + progress.progress(reports, {version: version + " (synthetic)" for version in self.project.versions})
            + "\n\n## Building\n"
        )
        (self.root / "README.md").write_text(document)
        base = test_policy()
        self.policy = replace(base, cores=2, state_root=self.directory / "state")
        from tests.cli.publication_tools import Tools

        self.tools = Tools(self)
        setup.run(self.project, self.policy)
        self.project = config.load(self.root)
        from unbake.layout import map as ownership

        def synthetic_map(selected):
            members = ownership.catalog(selected)
            return ownership.Map(
                2, tuple(ownership.Group(m.name, m.segment, "default", (m.name,)) for m in members.values())
            )

        mapping = patch.object(ownership, "load", side_effect=synthetic_map)
        mapping.start()
        self.addCleanup(mapping.stop)

        self.sources = []
        for name in names:
            source = self.project.drafts / name / (name + ".c")
            source.parent.mkdir(parents=True)
            source.write_text(f"int {name}(void) {{ return 1; }}\n")
            self.sources.append(source)
        self.manifest = self.root / "unbake-exclusions.json"
        self.manifest.write_text(json.dumps({"schema": 1, "functions": list(names)}) + "\n")
        self.script = Path(sysconfig.get_path("scripts")) / "unbake"
        self.env = dict(os.environ, PYTHONNOUSERSITE="1")
        self.env.pop("PYTHONPATH", None)
        self.write_policy()
        for version in self.project.versions:
            generation = self.project.build / (version + ".0")
            self.tools.prime(self.project, version, generation)
            self.project.build_link(version).symlink_to(generation.name)
        self.cli("map")
        self.cli("solve")

    def write_policy(self):
        path = self.directory / "policy.toml"
        values = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.policy).items()}
        path.write_text(toml.dumps(values))
        self.env["UNBAKE_POLICY"] = str(path)

    def cli(self, *arguments):
        if arguments and arguments[0] == "try" and "--scratch" not in arguments:
            arguments = (*arguments, "--scratch", str(self.directory / "scratch"))
        from unittest.mock import patch

        from tests.process_fakes import cli

        with patch.dict(os.environ, self.env):
            result = cli(["--project", self.root, *arguments])
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Traceback", output)
        return output

    def make(self, expected=0):
        for version in self.project.versions:
            generation = self.project.build_link(version).resolve()
            image = generation / f"{self.project.name}.{version}.z64"
            self.assertTrue(image.is_file())
            self.assertEqual(hashlib.sha1(image.read_bytes()).hexdigest(), self.project.version(version).baserom_sha1)
        return ": OK"

    def run_cli(self, arguments, **kwargs):
        from unittest.mock import patch

        from tests.process_fakes import cli

        with patch.dict(os.environ, kwargs.get("env", self.env)):
            return cli(arguments[1:])

    def inputs(self):
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for relative in ("config.toml", "Makefile", "tools", "include", "versions", "unbake-exclusions.json")
            for path in (
                [self.root / relative] if (self.root / relative).is_file() else (self.root / relative).rglob("*")
            )
            if path.is_file() and "__pycache__" not in path.parts
        }

    def install_sdk(self):
        (self.project.include[0] / "types.h").write_text(
            "#ifndef TYPES_H\n#define TYPES_H\ntypedef unsigned int u32; typedef unsigned long long u64;\n#endif\n"
        )
        setup._sdk_headers(self.project)

    def test_external_draft_receipt_pins_landed_bytes_through_repeated_solve(self):
        source = self.directory / "external-drafts/alpha.c"
        source.parent.mkdir()
        source.write_text(
            "/* NON_MATCHING: draft of alpha; verify behavior and bytes before match. */\n"
            "\n\n/* retained draft formatting */\nint alpha(void) {\n\n    return 1;\n}\n"
        )
        original = source.read_bytes()
        scratch = self.directory / "scratch"
        self.cli("try", source, "--scratch", scratch)
        self.cli("submit", source, "--scratch", scratch)
        published = self.project.src / source.name
        landed = published.read_bytes()
        self.assertNotEqual(original, landed)
        self.assertEqual(source.read_bytes(), original)
        proven_path = self.project.build / "types/proven.json"
        proven = proven_path.read_bytes()
        record = json.loads(proven)["records"]["alpha"]
        digest = hashlib.sha256(landed).hexdigest()
        self.assertEqual(record["source"], "src/alpha.c")
        self.assertEqual(record["source_sha256"], digest)
        self.assertEqual(record["proof"]["source_sha256"], digest)
        for _ in range(2):
            self.cli("solve")
            self.assertEqual(published.read_bytes(), landed)
            self.assertEqual(proven_path.read_bytes(), proven)

    def test_published_edit_requests_resubmit_and_refreshes_both_receipts(self):
        source = self.sources[0]
        self.cli("try", source)
        self.cli("submit", source)
        published = self.project.src / source.name
        landed = published.read_bytes()
        proven_path = self.project.build / "types/proven.json"
        proven = proven_path.read_bytes()
        published.write_bytes(landed + b"\n")
        result = self.run_cli(
            [str(self.script), "--project", str(self.root), "solve"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("published source changed", result.stdout + result.stderr)
        self.assertIn(f"submit {published}", result.stdout + result.stderr)
        self.assertEqual(proven_path.read_bytes(), proven)
        self.assertIn(f"submit {published}", self.cli("next"))
        compiled = len(self.tools.compiled)
        generations = {v: self.project.build_link(v).resolve() for v in self.project.versions}
        self.cli("submit", published)
        self.assertGreater(len(self.tools.compiled), compiled)
        for version in self.project.versions:
            self.assertNotEqual(self.project.build_link(version).resolve(), generations[version])
        record = json.loads(proven_path.read_bytes())["records"][source.stem]
        digest = hashlib.sha256(published.read_bytes()).hexdigest()
        self.assertEqual(record["source_sha256"], digest)
        self.assertEqual(record["proof"]["source_sha256"], digest)
        ledger = self.policy.state_root / self.project.id / self.project.workspace_id / "receipts/match.jsonl"
        publication = json.loads(ledger.read_text().splitlines()[-1])
        self.assertEqual(publication["source_sha256"], digest)
        self.cli("solve")

    def test_unchanged_published_source_is_noop_and_failed_edit_preserves_inputs(self):
        source = self.sources[0]
        self.cli("submit", source)
        published = self.project.src / source.name
        proven = self.project.build / "types/proven.json"
        ledger = self.policy.state_root / self.project.id / self.project.workspace_id / "receipts/match.jsonl"
        receipts = {p: p.read_bytes() for p in (proven, ledger)}
        generations = {v: self.project.build_link(v).resolve() for v in self.project.versions}
        compiled = len(self.tools.compiled)
        self.assertIn("published source unchanged", self.cli("submit", published))
        self.assertEqual(len(self.tools.compiled), compiled)
        published.write_text(published.read_text().replace("return 1", "return 2"))
        edited = published.read_bytes()
        result = self.run_cli([str(self.script), "--project", str(self.root), "submit", str(published)], env=self.env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("expected", result.stdout + result.stderr)
        self.assertIn("produced", result.stdout + result.stderr)
        self.assertEqual(published.read_bytes(), edited)
        self.assertEqual(receipts, {p: p.read_bytes() for p in receipts})
        self.assertEqual(generations, {v: self.project.build_link(v).resolve() for v in self.project.versions})

    def test_feedback_header_retirement_rebinds_source_and_dependencies_in_transaction(self):
        from unbake.layout import index
        from unbake.match import batch

        self.cli("submit", self.sources[0])
        published = self.project.src / "alpha.c"
        helper = self.project.include[0] / "common/draft_fields_alpha.h"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("struct Measured {int value;};\n")
        index.update(self.project, {helper: helper.read_text()})
        published.write_text('#include "common/draft_fields_alpha.h"\n' + published.read_text())
        for version in self.project.versions:
            generation = self.project.build_link(version).resolve()
            self.tools.compile(self.project, self.policy, [published], version, generation / "obj/src")
            obj = generation / "obj/src/alpha.o"
            obj.with_suffix(".d").write_text("target: src/alpha.c include/common/draft_fields_alpha.h\n")

        def retire(project, policy, candidates, previous, strict=False):
            lookup = index.load(project)
            lookup["headers"].pop("common/draft_fields_alpha.h")
            lookup["symbols"] = {
                name: home for name, home in lookup["symbols"].items() if home != "common/draft_fields_alpha.h"
            }
            helper.unlink()
            index.path(project).write_bytes(index.encoded(lookup))
            return []

        with patch.object(batch, "_feedback", side_effect=retire):
            self.cli("submit", published)
        self.assertFalse(helper.exists())
        self.assertNotIn("draft_fields_alpha", published.read_text())
        for version in self.project.versions:
            generation = self.project.build_link(version).resolve()
            obj = generation / "obj/src/alpha.o"
            self.assertTrue(obj.is_file())
            self.assertNotIn("draft_fields_alpha", obj.with_suffix(".d").read_text())
            self.assertEqual(obj.with_suffix(".built").stat().st_mtime_ns, 1)
        record = json.loads((self.project.build / "types/proven.json").read_text())["records"]["alpha"]
        self.assertEqual(record["source_sha256"], hashlib.sha256(published.read_bytes()).hexdigest())

    def test_republication_feedback_failure_restores_previous_receipts_and_file(self):
        from unittest.mock import patch

        from unbake.project.config import Held
        from unbake.typemap import storage

        source = self.sources[0]
        self.cli("submit", source)
        published = self.project.src / source.name
        published.write_bytes(published.read_bytes() + b"\n/* edited */\n")
        edited = published.read_bytes()
        proven = self.project.build / "types/proven.json"
        ledger = self.policy.state_root / self.project.id / self.project.workspace_id / "receipts/match.jsonl"
        receipts = {p: p.read_bytes() for p in (proven, ledger)}
        generations = {v: self.project.build_link(v).resolve() for v in self.project.versions}

        def failed_feedback(*args, **kwargs):
            storage.write(proven, b"partial replacement receipt")
            raise Held("submit", "types.feedback.target_sha256: injected failure")

        with patch("unbake.decomp.type_context.feedback_many", side_effect=failed_feedback):
            result = self.run_cli(
                [str(self.script), "--project", str(self.root), "submit", str(published)], env=self.env
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("types.feedback.target_sha256", result.stdout + result.stderr)
        self.assertEqual(published.read_bytes(), edited)
        self.assertEqual(receipts, {p: p.read_bytes() for p in receipts})
        self.assertEqual(generations, {v: self.project.build_link(v).resolve() for v in self.project.versions})

    def test_submit_succeeds_with_named_followup_for_another_stale_published_source(self):
        first, second = self.sources
        self.cli("submit", first)
        stale = self.project.src / first.name
        stale.write_bytes(stale.read_bytes() + b"\n/* edited */\n")
        output = self.cli("submit", second)
        self.assertIn("beta matched on VERSION", output)
        self.assertIn("OK(types):", output)
        self.assertNotIn("HELD(", output)
        self.assertIn(f"submit {stale}", output.split("Next: ", 1)[1])
        published = self.project.src / second.name
        records = json.loads((self.project.build / "types/proven.json").read_bytes())["records"]
        self.assertEqual(records["beta"]["source_sha256"], hashlib.sha256(published.read_bytes()).hexdigest())
        self.assertNotEqual(records["alpha"]["source_sha256"], hashlib.sha256(stale.read_bytes()).hexdigest())
        published.write_bytes(published.read_bytes() + b"\n/* another edit */\n")
        output = self.cli("submit", published)
        self.assertNotIn("HELD(", output)
        self.assertIn(f"submit {stale}", output.split("Next: ", 1)[1])
        records = json.loads((self.project.build / "types/proven.json").read_bytes())["records"]
        self.assertEqual(records["beta"]["source_sha256"], hashlib.sha256(published.read_bytes()).hexdigest())
        self.cli("submit", stale)
        self.cli("solve")

    def test_installed_submit_resolves_sdk_commands_and_callbacks_and_folds_shared_types(self):
        self.install_sdk()
        source = self.sources[0]
        source.write_text(
            '#include "audio_callbacks.h"\n'
            "struct Holder { Acmd command; ALCmdHandler callback; };\n"
            "int alpha(void) { return 1; }\n"
        )
        before = self.inputs()
        self.assertIn("exact words 2/2", self.cli("try", source))
        self.assertEqual(self.inputs(), before)
        output = self.cli("submit", source)
        self.assertIn("alpha matched on VERSION us, us-rev1", output)
        from unbake.layout import index

        header = next(path for path in index.headers(self.project) if "Acmd command;" in path.read_text())
        self.assertIn("Acmd command;", header.read_text())
        self.assertIn('#include "acmd.h"', header.read_text())
        self.assertIn('#include "audio_callbacks.h"', header.read_text())
        for name in ("gbi.h", "acmd.h", "audio_callbacks.h"):
            path = self.project.include[0] / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before["include/" + name])
        self.assertIn(": OK", self.make())

    def test_installed_batch_shares_one_new_sdk_header_overlay(self):
        from unbake.decomp import work

        self.install_sdk()
        for source in self.sources:
            work.overlay(self.project, source.parent)
            header = source.parent / "overlay/include/holder.h"
            header.write_text(
                '#ifndef HOLDER_H\n#define HOLDER_H\n#include "acmd.h"\n'
                "typedef struct { Acmd command; } Holder;\n#endif\n"
            )
            source.write_text(f'#include "holder.h"\nint {source.stem}(void) {{ return 1; }}\n')
            self.cli("try", source)
        output = self.cli("submit", "--batch", *self.sources)
        for source in self.sources:
            self.assertIn(source.stem + " matched on VERSION us, us-rev1", output)
        from unbake.layout import index

        self.assertTrue(any("Acmd command;" in path.read_text() for path in index.headers(self.project)))
        self.assertIn(": OK", self.make())

    def test_installed_try_reuses_a_header_already_published_from_another_overlay(self):
        from unbake.decomp import work

        self.install_sdk()
        for source in self.sources:
            work.overlay(self.project, source.parent)
            (source.parent / "overlay/include/holder.h").write_text(
                '#ifndef HOLDER_H\n#define HOLDER_H\n#include "acmd.h"\n'
                "typedef struct { Acmd command; } Holder;\n#endif\n"
            )
            source.write_text(f'#include "holder.h"\nint {source.stem}(void) {{ return 1; }}\n')
        self.cli("try", self.sources[0])
        self.cli("submit", self.sources[0])
        self.cli("try", self.sources[1])
        self.cli("submit", self.sources[1])
        self.assertIn(": OK", self.make())

    def test_installed_submit_names_missing_sdk_header_prerequisite_for_exact_source(self):
        self.install_sdk()
        path = self.project.include[0] / "missing_sdk.h"
        path.write_text("typedef struct { MissingSDK words; } MissingRecord;\n")
        source = self.sources[0]
        self.assertIn("exact words 2/2", self.cli("try", source))
        before = self.inputs()
        for operands in (("submit", source), ("submit", "--batch", source)):
            result = self.run_cli(
                [str(self.script), "--project", str(self.root), *map(str, operands)],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1, output)
            self.assertIn("headers.declaration: SDK/shared header prerequisite", output)
            self.assertIn("MissingSDK: missing type layout", output)
            self.assertIn("include/missing_sdk.h:1", output)
            self.assertIn("Next: Repair the SDK/shared header prerequisite", output)
            self.assertNotIn("Supply alpha", output)
            self.assertEqual(self.inputs(), before)
            self.assertFalse((self.project.src / source.name).exists())

    def test_equivalent_batch_is_read_only_until_one_proved_publication(self):
        before = self.inputs()
        for source in self.sources:
            output = self.cli("try", source)
            self.assertIn(f"retained {source.stem}", output)
            self.assertEqual(self.inputs(), before)
        output = self.cli("submit", "--batch", *self.sources)
        self.assertIn("alpha matched", output)
        self.assertIn("beta matched", output)
        # config.toml stays configuration only: measurements never become selections.
        data = toml.loads((self.root / "config.toml").read_text())
        self.assertNotIn("compiler_selections", data)
        self.assertEqual(data["units"], {source.stem: "ido-5.3" for source in self.sources})
        for source in self.sources:
            self.assertTrue((self.project.src / source.name).is_file())
        self.assertEqual(json.loads(self.manifest.read_bytes())["functions"], [])
        self.assertIn(": OK", self.make())
        # The same cartridge remains exact under either measured compiler member.
        for member in ("ido-5.3", "ido-7.1"):
            # [units] lists only exceptions; the default compiler needs no entry.
            data["units"] = (
                {} if member == data["project"]["default_compiler"] else {s.stem: member for s in self.sources}
            )
            if not data["units"]:
                del data["units"]
            (self.root / "config.toml").write_text(toml.dumps(data))
            setup.run(config.load(self.root), self.policy)
            self.assertIn(": OK", self.make())

    def test_failed_cartridge_proof_preserves_compiler_config_and_exclusions(self):
        for source in self.sources:
            self.cli("try", source)
        before = self.inputs()
        broken = self.directory / "refuse-link"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        original = self.policy
        self.policy = replace(self.policy, mips_ld=broken)
        self.write_policy()
        result = self.run_cli(
            [str(self.script), "--project", str(self.root), "submit", "--batch", *map(str, self.sources)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cartridge differs on", result.stdout)
        self.assertEqual(self.inputs(), before)
        self.assertFalse(list(self.project.src.glob("*.c")))
        self.policy = original
        self.write_policy()
        self.cli("submit", "--batch", *self.sources)
        self.assertEqual(json.loads(self.manifest.read_bytes())["functions"], [])

    def test_try_overlaps_submit_proof_and_both_sources_publish(self):
        self.overlap(finishes_during_proof=True)

    def test_submit_publishes_between_another_sources_candidate_measurements(self):
        self.overlap(finishes_during_proof=False)

    def overlap(self, *, finishes_during_proof):
        self.cli("try", self.sources[0])
        before = (self.root / "config.toml").read_bytes()
        observed = []

        def while_proving(project, names):
            self.tools.proof_hook = None
            with (self.project.build / ".lock").open("a+b") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual((self.root / "config.toml").read_bytes(), before)
            if finishes_during_proof:
                self.tools.try_source(self.project, self.policy, self.sources[1], self.project.work)
            observed.append(tuple(names))

        self.tools.proof_hook = while_proving
        if not finishes_during_proof:
            self.cli("try", self.sources[1])
        self.assertIn("alpha matched", self.cli("submit", self.sources[0]))
        self.assertTrue(observed)
        self.assertIn("beta matched", self.cli("submit", self.sources[1]))
        self.assertEqual(json.loads(self.manifest.read_bytes())["functions"], [])
