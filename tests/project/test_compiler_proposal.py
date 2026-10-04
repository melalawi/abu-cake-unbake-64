"""Compiler evidence, exact review tokens and refusals before publication."""

import contextlib
import hashlib
import io
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project.test_rom import BOOTCODES, cartridge
from unbake.project import compiler_profiles, compiler_proposal, config, fingerprint, header, init, rom, toolchain
from unbake.project.census import Census
from unbake.project.flow import LayoutManifest


def instructions(*words: int) -> bytes:
    return struct.pack(">" + "I" * len(words), *words)


GCC = instructions(*([0x00801021] * 10), 0x27BD0018, 0x03E00008, 0)
SN64 = instructions(*([0x00801021] * 10), 0x03E00008, 0x27BD0018)
IDO = instructions(*([0x00801025] * 10), 0x03E00008, 0)
LEAF = instructions(0x03E00008, 0)


class ProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.rom_fixture import install

        install(self)
        from tests.process_fakes import boundary, git_init

        git = boundary(init, git_init)
        git.start()
        from unbake.project import hygiene

        empty_index = boundary(
            hygiene, lambda command, **kwargs: __import__("subprocess").CompletedProcess(command, 0, b"", b"")
        )
        empty_index.start()
        self.addCleanup(empty_index.stop)
        self.addCleanup(git.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        with contextlib.redirect_stdout(io.StringIO()):
            init.run(self.base / "game", layout_cap=2)
        self.project = config.load_pending(self.base / "game")
        self.policy = config.SetupPolicy(
            0.1,
            1,
            self.base / "cache",
            self.base / "splat",
            self.base / "as",
            self.base / "ld",
            self.base / "objcopy",
            self.base / "cpp",
            (),
            (),
            (),
            5,
            4,
            0.9,
            0.1,
        )
        self.initial_config = (self.project.root / "config.toml").read_bytes()

    def layout(self, *bodies: bytes) -> tuple[Census, LayoutManifest]:
        code = b"".join(bodies)
        data = cartridge(instructions=code)
        path = self.project.roms / "baserom.us.z64"
        path.write_bytes(data)
        cartridge_rom = rom.Rom(path, data, header.parse(data, BOOTCODES), hashlib.sha1(data).hexdigest())
        census = Census((cartridge_rom,), {path: "us"}, "us", {}, {}, self.project.build / "setup/roms.json")
        functions = []
        offset = 0x1000
        for index, body in enumerate(bodies):
            functions.append(
                {
                    "name": f"f{index}",
                    "start": offset,
                    "end": offset + len(body),
                    "address": 0x80000000 + offset,
                    "evidence": {"boundary": "proved"},
                }
            )
            offset += len(body)
        layout: LayoutManifest = {
            "schema": 1,
            "project_id": self.project.id,
            "checkout_id": self.project.checkout_id,
            "rom_sha1": {"us": cartridge_rom.sha1},
            "names_from": "us",
            "inputs_sha256": {},
            "versions": {"us": {"functions": functions, "providers": [], "loaded_spans": [], "evidence": {}}},
        }
        return census, layout

    def propose(self, census: Census, layout: LayoutManifest, choices: dict[str, str] | None = None):
        return fingerprint.propose_compilers(self.project, census, layout, self.policy, choices=choices)

    def accept(self, census: Census, layout: LayoutManifest, proposal, confirm: str | None = None) -> None:
        fingerprint.confirm_proposal(self.project, census, layout, proposal, self.policy, confirm=confirm)

    def token(self) -> str:
        return hashlib.sha256(compiler_proposal.proposal_path(self.project).read_bytes()).hexdigest()

    def unchanged(self) -> None:
        self.assertEqual((self.project.root / "config.toml").read_bytes(), self.initial_config)
        self.assertFalse(self.project.tools.exists())
        self.assertFalse((self.project.root / "versions").exists())
        self.assertFalse((self.project.root / "docs").exists())

    def test_mixed_compilers_keep_ido_tie_with_displayed_default(self) -> None:
        census, layout = self.layout(GCC, IDO, LEAF)
        proposal = self.propose(census, layout)
        self.assertEqual(proposal["candidates"]["us:gcc"][0]["id"], "gcc-2.7.2-kmc")
        self.assertEqual(proposal["candidates"]["us:ido"][0]["rank"], proposal["candidates"]["us:ido"][1]["rank"])
        # A tie compiles the first tied member in registry order; try ranks the rest.
        self.assertEqual(sorted(proposal["first_try"]["us:ido"]), ["ido-5.3", "ido-7.1"])
        self.assertEqual(proposal["assignments"]["f1"], proposal["first_try"]["us:ido"][0])
        self.assertNotIn("default:mixed", proposal["unresolved"])
        self.assertEqual(proposal["default_compiler"], "gcc-2.7.2-kmc")
        self.accept(census, layout, proposal, self.token())
        choices = {"us:ido": "ido-7.1", "default": "gcc-2.7.2-kmc"}
        proposal = self.propose(census, layout, choices)
        self.assertEqual(proposal["assignments"]["f0"], "gcc-2.7.2-kmc")
        self.assertEqual(proposal["assignments"]["f1"], "ido-7.1")
        self.assertEqual(proposal["unresolved"], [])
        self.accept(census, layout, proposal, self.token())
        self.unchanged()

    def test_uninformative_unit_does_not_inherit_a_ranked_region(self) -> None:
        census, layout = self.layout(SN64, LEAF)
        proposal = self.propose(census, layout)
        # An uninformative unit builds with the default first; it inherits no region.
        self.assertEqual(proposal["assignments"]["f1"], proposal["default_compiler"])
        self.accept(census, layout, proposal, self.token())
        self.unchanged()

    def test_weak_nonzero_evidence_gets_independent_all_family_sets(self) -> None:
        census, layout = self.layout(instructions(0x00801021, 0x03E00008, 0), LEAF)
        proposal = self.propose(census, layout, {"default": "gcc-2.7.2-kmc"})
        # Weak evidence never pins a unit: both build with the explicit default first.
        self.assertEqual(proposal["assignments"], {"f0": "gcc-2.7.2-kmc", "f1": "gcc-2.7.2-kmc"})

    def test_tie_accepts_explicit_candidate_set_without_selecting_registry_order(self) -> None:
        census, layout = self.layout(IDO)
        proposal = self.propose(census, layout)
        self.assertEqual(sorted(proposal["first_try"]["us:ido"]), ["ido-5.3", "ido-7.1"])
        self.assertEqual(proposal["assignments"], {"f0": proposal["first_try"]["us:ido"][0]})
        self.assertTrue(all(len(rows) == 4 for rows in proposal["candidates"].values()))
        self.assertTrue(any("setup --confirm" in line for line in fingerprint.receipt(proposal)))
        tie = "{" + ", ".join(proposal["first_try"]["us:ido"]) + "}"
        self.assertTrue(any(f"bytes tie {tie}" in line for line in fingerprint.receipt(proposal)))
        self.accept(census, layout, proposal, self.token())
        self.unchanged()

    def test_real_probe_match_separates_static_release_tie(self) -> None:
        from unbake.project import compiler_probes

        census, layout = self.layout(IDO)
        report = {
            "attempted": 4,
            "successful_comparable": 4,
            "errors": [],
            "candidates": {"ido-5.3": {"score": [0, 0]}, "ido-7.1": {"score": [1, 1]}},
        }
        with patch.object(compiler_probes, "reproduce", return_value=report) as reproduce:
            proposal = self.propose(census, layout)
        self.assertEqual(proposal["assignments"], {"f0": "ido-7.1"})
        self.assertNotIn("us:ido", proposal.get("first_try", {}))
        self.assertEqual(proposal["source_reproduction_probes"]["successful_comparable"], 4)
        self.assertEqual(reproduce.call_args.args[2], ["ido-5.3", "ido-7.1"])
        self.accept(census, layout, proposal, self.token())

    def test_incomplete_probes_never_turn_compile_failure_into_a_choice(self) -> None:
        from unbake.project import compiler_probes

        census, layout = self.layout(IDO)
        report = {
            "attempted": 4,
            "successful_comparable": 2,
            "errors": [{"candidate": "ido-5.3", "reason": "missing binary"}],
            "candidates": {"ido-5.3": {"score": [0, 0]}, "ido-7.1": {"score": [1, 1]}},
        }
        with patch.object(compiler_probes, "reproduce", return_value=report):
            proposal = self.propose(census, layout)
        self.assertEqual(sorted(proposal["first_try"]["us:ido"]), ["ido-5.3", "ido-7.1"])
        self.assertEqual(proposal["assignments"], {"f0": proposal["first_try"]["us:ido"][0]})

    def test_clear_regional_release_is_a_confirmable_whole_proposal(self) -> None:
        census, layout = self.layout(SN64, SN64, GCC)
        proposal = self.propose(census, layout)
        self.assertEqual(set(proposal["assignments"].values()), {"gcc-2.8.1-sn64"})
        self.assertEqual(proposal["unresolved"], [])
        self.assertEqual(proposal["choices"], {})
        self.accept(census, layout, proposal, self.token())
        self.unchanged()

    def test_confirmation_guard_retains_pins_and_refuses_changed_or_missing_inputs(self) -> None:
        census, layout = self.layout(GCC)
        path = self.project.build / "setup/layout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = compiler_proposal.encoded(layout)
        path.write_bytes(original)
        proposal = compiler_proposal.propose_compilers(self.project, census, layout, self.policy)
        guard = compiler_proposal.confirmation_guard(self.project, proposal, self.policy)
        guard()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale"):
            guard()
        path.write_bytes(original)
        proposal_path = compiler_proposal.proposal_path(self.project)
        content = proposal_path.read_bytes()
        proposal_path.write_bytes(content + b" ")
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale"):
            guard()
        proposal_path.unlink()
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale"):
            guard()

    def test_exact_prologue_words_and_interpreter_digest_are_evidence(self) -> None:
        census, layout = self.layout(SN64)
        proposal = self.propose(census, layout)
        measured = proposal["regions"]["us:gcc"][0]
        self.assertEqual(measured["prologue_words"][0], {"rom_offset": 0x1000, "word": "0x00801021"})
        self.assertIn("evidence_engine", proposal["inputs_sha256"])

    def add_version(self, census: Census, layout: LayoutManifest, body: bytes, name: str) -> Census:
        data = cartridge(revision=1, instructions=body)
        path = self.project.roms / "baserom.us-rev1.z64"
        path.write_bytes(data)
        image = rom.Rom(path, data, header.parse(data, BOOTCODES), hashlib.sha1(data).hexdigest())
        layout["rom_sha1"]["us-rev1"] = image.sha1
        layout["versions"]["us-rev1"] = {
            "functions": [
                {"name": name, "start": 0x1000, "end": 0x1000 + len(body), "address": 0x80001000, "evidence": {}}
            ],
            "providers": [],
            "loaded_spans": [],
            "evidence": {},
        }
        return Census((*census.cartridges, image), {**census.names, path: "us-rev1"}, "us", {}, {}, census.manifest)

    def test_item_absent_from_naming_version_still_gets_compiler_evidence(self) -> None:
        census, layout = self.layout(SN64)
        census = self.add_version(census, layout, SN64, "only_rev1")
        proposal = self.propose(census, layout)
        self.assertEqual(proposal["assignments"], {"f0": "gcc-2.8.1-sn64", "only_rev1": "gcc-2.8.1-sn64"})
        self.assertEqual(proposal["unresolved"], [])
        self.accept(census, layout, proposal, self.token())
        self.unchanged()

    def test_conflicting_holding_versions_defer_to_measured_candidates(self) -> None:
        census, layout = self.layout(SN64)
        census = self.add_version(census, layout, GCC, "f0")
        # Contradictory holding versions pin nothing; the unit waits for an explicit default.
        self.assertEqual(self.propose(census, layout)["unresolved"], ["default:missing"])
        proposal = self.propose(census, layout, {"default": "gcc-2.7.2-kmc"})
        self.assertEqual(proposal["unresolved"], [])
        self.assertEqual(proposal["assignments"]["f0"], "gcc-2.7.2-kmc")
        self.accept(census, layout, proposal, self.token())
        reviewed = self.propose(census, layout, {"f0": "gcc-2.8.1-sn64"})
        self.accept(census, layout, reviewed, self.token())
        self.unchanged()

    def test_explicit_choices_persist_and_digest_covers_exact_utf8_bytes(self) -> None:
        census, layout = self.layout(IDO)
        chosen = self.propose(census, layout, {"us:ido": "ido-7.1"})
        token = self.token()
        repeated = self.propose(census, layout)
        self.assertEqual(chosen, repeated)
        self.assertEqual(token, self.token())
        self.assertIn(token, "\n".join(fingerprint.receipt(repeated)))
        self.accept(census, layout, repeated, token)
        accepted = json.loads(compiler_proposal.proposal_path(self.project).read_bytes())
        self.assertNotIn("digest", accepted)
        self.unchanged()

    def test_non_tty_missing_confirmation_prints_command_and_writes_no_assignments(self) -> None:
        census, layout = self.layout(SN64)
        proposal = self.propose(census, layout)
        with (
            patch("sys.stdin.isatty", return_value=False),
            self.assertRaisesRegex(config.Held, f"setup.compiler_confirmation:.*setup --confirm {self.token()}"),
        ):
            self.accept(census, layout, proposal)
        self.unchanged()

    def test_tty_reject_eof_interrupt_and_yes(self) -> None:
        census, layout = self.layout(SN64)
        proposal = self.propose(census, layout)
        for answer in ("no", "", "y", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=repr(answer)), patch("sys.stdin.isatty", return_value=True):
                kwargs = {"side_effect": answer} if isinstance(answer, BaseException) else {"return_value": answer}
                with (
                    patch("builtins.input", **kwargs),
                    self.assertRaisesRegex(config.Held, "setup.compiler_confirmation:"),
                ):
                    self.accept(census, layout, proposal)
                self.unchanged()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="yes"):
            self.accept(census, layout, proposal)
        self.unchanged()

    def test_changes_to_rom_policy_registry_layout_choice_or_persisted_bytes_are_stale(self) -> None:
        census, layout = self.layout(SN64)
        proposal = self.propose(census, layout)
        token = self.token()
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale:"):
            self.accept(census, layout, proposal, "0" * 64)
        original = census.cartridges[0].path.read_bytes()
        census.cartridges[0].path.write_bytes(original[:-4] + b"edit")
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale:"):
            self.accept(census, layout, proposal, token)
        census.cartridges[0].path.write_bytes(original)
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale:"):
            fingerprint.confirm_proposal(
                self.project, census, layout, proposal, replace(self.policy, probe_count=9), confirm=token
            )
        altered = self.base / "registry.toml"
        altered.write_bytes(toolchain.REGISTRY_PATH.read_bytes() + b"\n# changed\n")
        with (
            patch.object(toolchain, "REGISTRY_PATH", altered),
            self.assertRaisesRegex(config.Held, "setup.proposal_stale:"),
        ):
            self.accept(census, layout, proposal, token)
        layout["versions"]["us"]["functions"][0]["evidence"]["new"] = True
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale:"):
            self.accept(census, layout, proposal, token)
        del layout["versions"]["us"]["functions"][0]["evidence"]["new"]
        changed = self.propose(census, layout, {"us:gcc": "gcc-2.7.2-kmc"})
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale:"):
            self.accept(census, layout, changed, token)
        persisted = compiler_proposal.proposal_path(self.project)
        persisted.write_bytes(persisted.read_bytes() + b" ")
        with self.assertRaisesRegex(config.Held, "setup.proposal_stale:"):
            self.accept(census, layout, changed, self.token())
        self.unchanged()

    def test_layout_file_or_config_edited_during_tty_confirmation_is_stale(self) -> None:
        census, layout = self.layout(SN64)
        layout_file = self.project.build / "setup/layout.json"
        layout_file.parent.mkdir(parents=True)
        layout_file.write_bytes(compiler_proposal.encoded(layout))
        proposal = self.propose(census, layout)
        with patch("sys.stdin.isatty", return_value=True):

            def answer(prompt):
                layout_file.write_bytes(layout_file.read_bytes() + b" ")
                return "yes"

            with (
                patch("builtins.input", side_effect=answer),
                self.assertRaisesRegex(config.Held, "setup.proposal_stale:"),
            ):
                self.accept(census, layout, proposal)
        self.unchanged()

    def test_unknown_registry_id_unknown_region_missing_functions_and_overlap_are_named(self) -> None:
        census, layout = self.layout(SN64)
        for choices in ({"us:gcc": "unknown"}, {"missing": "ido-7.1"}):
            with self.assertRaisesRegex(config.Held, "setup.compiler_candidate:"):
                self.propose(census, layout, choices)
        layout["versions"]["us"]["functions"] = []
        with self.assertRaisesRegex(config.Held, "setup.compiler_proposal:.*functions missing"):
            self.propose(census, layout)
        census, layout = self.layout(SN64, SN64)
        layout["versions"]["us"]["functions"][1]["start"] -= 4
        with self.assertRaisesRegex(config.Held, "setup.compiler_proposal:.*overlapping"):
            self.propose(census, layout)
        self.unchanged()

    def test_missing_layout_inputs_are_named_before_proposal_writes(self) -> None:
        for key in ("schema", "inputs_sha256"):
            with self.subTest(key=key):
                census, layout = self.layout(SN64)
                del layout[key]
                with self.assertRaisesRegex(config.Held, f"setup.compiler_proposal: layout.{key}: missing input"):
                    self.propose(census, layout)
        census, layout = self.layout(SN64)
        del layout["versions"]["us"]["functions"][0]["address"]
        with self.assertRaisesRegex(config.Held, "setup.compiler_proposal:.*functions.0.address: missing input"):
            self.propose(census, layout)
        self.assertFalse(compiler_proposal.proposal_path(self.project).exists())
        self.unchanged()

    def test_profiles_retain_pin_flags_source_and_relocation_only_masks(self) -> None:
        profiles, _ = compiler_profiles.read()
        self.assertEqual(set(profiles), set(toolchain.registry()))
        saved = next(example for example in profiles["gcc-2.7.2-kmc"].exemplars if example.name == "saved")
        changed = tuple(word | 0x1234 if mask else word for word, mask in zip(saved.words, saved.masks, strict=True))
        self.assertTrue(saved.matches(changed))
        self.assertFalse(saved.matches((changed[0] ^ 0x10000, *changed[1:])))
        source = self.base / "profiles.toml"
        source.write_text(toolchain.REGISTRY_PATH.read_text().replace('abi = "gp32"', 'abi = "gp64"'))
        with (
            patch.object(toolchain, "REGISTRY_PATH", source),
            self.assertRaisesRegex(config.Held, "setup.compiler_profile:.*abi"),
        ):
            compiler_profiles.read()
