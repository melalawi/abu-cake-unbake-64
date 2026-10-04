"""ROM ingestion, durable restart records and explicit naming choices."""

import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project.test_rom import BOOTCODES, CODE, cartridge
from unbake import config
from unbake.project import census, header, init, setup_config
from unbake.config import CensusPolicy, Held


class CensusTests(unittest.TestCase):
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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "unrelated"
        init.run(self.root, layout_cap=2)
        self.project = config.load_pending(self.root)
        table = patch.object(header, "RETAIL", BOOTCODES)
        table.start()
        self.addCleanup(table.stop)
        measured = patch.object(
            census,
            "measured_code",
            side_effect=lambda item: (
                type("Range", (), {"start": 0x1000, "end": 0x1000 + len(CODE), "address": 0x80001000})(),
            ),
        )
        measured.start()
        self.addCleanup(measured.stop)

    def supply(self, name: str, **fields: object) -> Path:
        target = self.project.roms / name
        target.write_bytes(cartridge(**fields))
        return target

    def run_census(self, selected: str | None = "us") -> census.Census:
        with redirect_stdout(io.StringIO()):
            return census.run(self.project, CensusPolicy(0.1), names_from=selected)

    def test_header_order_full_matrix_and_fact_writer_preserve_pending_state(self) -> None:
        originals = [self.supply("not-a-region", region="E"), self.supply("first", region="P")]
        before = {path: path.read_bytes() for path in originals}
        result = self.run_census("us")
        self.assertEqual(result.versions, ("eu", "us"))
        document = json.loads(result.manifest.read_text())
        self.assertEqual(document["matrix"], {"eu": {"eu": 1.0, "us": 1.0}, "us": {"eu": 1.0, "us": 1.0}})
        self.assertTrue(document["ingestion_complete"])
        self.assertTrue(all(row["crc_validated"] for row in document["roms"]))
        setup_config.write_facts(self.project, result)
        pending = config.load_pending(self.root)
        self.assertEqual(pending.id, self.project.id)
        self.assertEqual(pending.state, "awaiting-roms")
        self.assertNotIn("compiler", (self.root / "config.toml").read_text())
        data = tomllib.loads((self.root / "config.toml").read_text())
        self.assertEqual(data["version"]["us"]["macros"], ["VERSION_US"])
        self.assertEqual(data["version"]["eu"]["macros"], ["VERSION_EU"])
        self.assertEqual(before, {path: path.read_bytes() for path in originals})
        with self.assertRaisesRegex(Held, "project.state"):
            config.load(self.root)

    def test_restart_skips_generated_inputs_but_refuses_additional_duplicate(self) -> None:
        source = self.supply("original")
        first = self.run_census()
        second = self.run_census()
        self.assertEqual(first.versions, second.versions)
        duplicate = self.project.roms / "duplicate"
        duplicate.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(Held, "setup.roms.duplicate_sha1"):
            self.run_census()

    def test_missing_reference_and_label_collision_leave_config_unchanged(self) -> None:
        original = (self.root / "config.toml").read_bytes()
        self.supply("one")
        with patch("sys.stdin.isatty", return_value=False), self.assertRaisesRegex(Held, "project.names_from"):
            self.run_census(None)
        self.assertFalse((self.project.build / "setup/roms.json").exists())
        self.supply("two", seed=1)
        with self.assertRaisesRegex(Held, "setup.roms.duplicate_version"):
            self.run_census()
        self.assertEqual((self.root / "config.toml").read_bytes(), original)

    def test_ready_project_rejects_changed_rom_set(self) -> None:
        self.supply("one")
        result = self.run_census()
        setup_config.write_facts(self.project, result)
        path = self.root / "config.toml"
        path.write_text(path.read_text().replace('state = "awaiting-roms"', 'state = "ready"'))
        self.project = config.load_pending(self.root)
        self.supply("two", region="P")
        with self.assertRaisesRegex(Held, "setup.rom_set_changed"):
            self.run_census()

    def test_failed_destination_does_not_publish_a_manifest_or_hide_inputs(self) -> None:
        self.supply("source")
        destination = self.project.roms / "baserom.us.z64"
        destination.write_bytes(b"bad")
        with self.assertRaisesRegex(Held, "rom.magic"):
            self.run_census()
        self.assertFalse((self.project.build / "setup/roms.json").exists())
        self.assertIn(destination, census.candidates(self.project))

    def test_byte_swapped_original_is_preserved_and_in_place_normalization_refuses(self) -> None:
        source = self.project.roms / "source.v64"
        normal = cartridge()
        swapped = bytearray(normal)
        swapped[0::2], swapped[1::2] = normal[1::2], normal[0::2]
        source.write_bytes(swapped)
        self.run_census()
        self.assertEqual(source.read_bytes(), swapped)
        target = self.project.roms / "baserom.us.z64"
        self.assertEqual(target.read_bytes(), normal)
        source.unlink()
        target.write_bytes(swapped)
        with self.assertRaisesRegex(Held, "setup.roms.destination"):
            self.run_census()
        self.assertEqual(target.read_bytes(), swapped)

    def test_tty_naming_choice_eof_and_invalid_values(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="eu"):
            self.assertEqual(census.naming_version(("eu", "us"), None), "eu")
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
            self.assertRaisesRegex(Held, "project.names_from"),
        ):
            census.naming_version(("us",), None)
        with self.assertRaisesRegex(Held, "project.names_from"):
            census.naming_version(("us",), "unknown")

    def test_incompatible_and_escaping_ingest_manifests_are_named(self) -> None:
        self.supply("one")
        manifest = self.project.build / "setup/roms.json"
        manifest.parent.mkdir(parents=True)
        for document in (
            [],
            {"schema": 2},
            {
                "schema": 1,
                "project_id": self.project.id,
                "checkout_id": self.project.checkout_id,
                "names_from": "us",
                "ingestion_complete": True,
                "generated_inputs": [
                    {"original_path": "../elsewhere", "normalized_path": "roms/input", "sha1": "0" * 40}
                ],
                "renames": {},
                "version_order": None,
            },
        ):
            manifest.write_text(json.dumps(document))
            with self.subTest(document=document), self.assertRaisesRegex(Held, "setup.roms.manifest"):
                census.candidates(self.project)
        manifest.unlink()
        with self.assertRaisesRegex(Held, "project.state"):
            setup_config.write_facts(replace(self.project, state="ready"), self.run_census())

    def test_foreign_workspace_manifest_and_symlinked_setup_folder_refuse(self) -> None:
        self.supply("one")
        result = self.run_census()
        document = json.loads(result.manifest.read_text())
        document["project_id"] = "00000000-0000-4000-8000-000000000000"
        result.manifest.write_text(json.dumps(document))
        with self.assertRaisesRegex(Held, "setup.roms.manifest"):
            census.candidates(self.project)
        result.manifest.unlink()
        result.manifest.parent.rmdir()
        outside = self.root / "authored"
        outside.mkdir()
        result.manifest.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Held, "setup.roms.manifest"):
            self.run_census()
        self.assertEqual(list(outside.iterdir()), [])

    def test_normalization_failure_rolls_back_new_copies_and_preserves_sources(self) -> None:
        from unbake.project import compiler_files

        sources = [self.supply("us-input"), self.supply("pal-input", region="P")]
        before = {path: path.read_bytes() for path in sources}
        original = compiler_files.atomic_bytes

        def failing(path: Path, data: bytes) -> None:
            if path.name == "baserom.us.z64":
                raise OSError("injected publication failure")
            original(path, data)

        with patch.object(compiler_files, "atomic_bytes", side_effect=failing), self.assertRaises(OSError):
            self.run_census()
        self.assertEqual(before, {path: path.read_bytes() for path in sources})
        self.assertFalse((self.project.roms / "baserom.eu.z64").exists())
        self.assertFalse((self.project.build / "setup/roms.json").exists())

    def test_saved_aliases_and_order_survive_restart_and_alias_revision(self) -> None:
        self.supply("one")
        self.supply("two", region="P")
        with redirect_stdout(io.StringIO()):
            first = census.run(
                self.project, CensusPolicy(0.1), names_from="us", renames={"eu": "pal"}, order=("us", "pal")
            )
        restarted = self.run_census()
        self.assertEqual(restarted.versions, first.versions)
        with redirect_stdout(io.StringIO()):
            changed = census.run(self.project, CensusPolicy(0.1), names_from="us", renames={"eu": "europe"})
        self.assertEqual(changed.versions, ("europe", "us"))
        self.assertEqual(self.run_census().versions, changed.versions)
        self.assertEqual(len(census.candidates(self.project)), 2)

    def test_empty_explicit_name_or_title_does_not_use_a_fallback(self) -> None:
        self.supply("one")
        result = self.run_census()
        for fields, key in (
            ({"name": "", "title": None}, "project.name"),
            ({"name": None, "title": ""}, "project.title"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(Held, key):
                setup_config.facts(self.project, result, **fields)

    def test_generated_version_macros_are_valid_and_collisions_refuse(self) -> None:
        self.assertEqual(
            setup_config.version_macros(("us-rev1", "eu-x", "2.extra")),
            {"us-rev1": "VERSION_US_REV1", "eu-x": "VERSION_EU_X", "2.extra": "VERSION_2_EXTRA"},
        )
        for versions in (("eu-x", "eu_x"), ("us", "US"), ("one.two", "one-two")):
            with self.subTest(versions=versions), self.assertRaisesRegex(Held, "project.versions"):
                setup_config.version_macros(versions)
