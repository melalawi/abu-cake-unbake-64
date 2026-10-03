"""Explicit naming facts and all-version make dispatch on tiny fixtures."""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project import config


class NamingConfigTests(unittest.TestCase):
    def test_names_from_is_required_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "config.toml"
            original = (
                '[project]\nname = "fixture"\ntitle = "Small project"\nnames_from = "'
                'us"\nversions = ["us", "eu-x"]\ndefault_compiler = "ido-7.1"\n[paths'
                ']\nsrc = "src"\ninclude = ["include"]\nasm = "asm"\ntools = "tools"\n['
                'compilers."ido-7.1"]\ncflags = []\n[units]\n[version.us]\nbaserom_sha'
                '1 = "21758de98b69907bcac48b82f833f14833a5f7cc"\nsplit = "versions/'
                'us/game.yaml"\nsymbols = "versions/us/symbol_addrs.txt"\nmacros = ['
                ']\n[version.eu-x]\nbaserom_sha1 = "eafd5564f7042dd6128314aadcb802db'
                'b9ff8cfa"\nsplit = "versions/eu-x/game.yaml"\nsymbols = "versions/e'
                'u-x/symbol_addrs.txt"\nmacros = []\n'
            )
            original = "schema = 1\n" + original.replace(
                "[project]", '[project]\nid = "00000000-0000-4000-8000-000000000001"\nstate = "ready"'
            )
            original = original.replace(
                "[paths]",
                '[workspace]\nid = "00000000-0000-4000-8000-000000000002"\n[paths]\n'
                'roms = "roms"\nbuild = "build"\nwork = "build/work"\ndrafts = "build/drafts"',
            )
            for version in ("us", "eu-x"):
                original = original.replace(
                    "[version." + version + "]",
                    "[version." + version + ']\nbaserom = "roms/baserom.' + version + '.z64"',
                )
            path.write_text(original)
            self.assertEqual(config.load(root).names_from, "us")
            self.assertFalse(hasattr(config.load(root), "default_version"))
            for value in ("", 'names_from = "missing"\n', "names_from = false\n"):
                with self.subTest(value=value):
                    path.write_text(original.replace('names_from = "us"\n', value))
                    with self.assertRaisesRegex(config.Held, "names_from"):
                        config.load(root)


class VersionBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name).resolve()
        project, _ = fixture(self.root, case=self)
        source = project.version("us")
        destination = self.root / "versions/eu-x"
        shutil.copytree(source.split.parent, destination)
        second = replace(
            source,
            name="eu-x",
            baserom=self.root / "baserom.eu-x.z64",
            split=destination / "game.yaml",
            symbols=destination / "symbol_addrs.txt",
        )
        second.baserom.write_bytes(b"ABC")
        self.project = replace(
            project, names_from="eu-x", versions=("us", "eu-x"), version_map={"us": source, "eu-x": second}
        )
        write_rendered(self.project)
        (destination / "game.sha1").write_text(second.baserom_sha1 + "  build/eu-x/game.eu-x.z64\n")
        (destination / "baserom.sha1").write_text(second.baserom_sha1 + "  baserom.eu-x.z64\n")

    def test_plain_make_and_check_select_all_versions(self):
        text = (self.root / "Makefile").read_text()
        self.assertIn("us eu-x", text)
        self.assertIn("COMPARE ?= 1", text)
        self.assertIn("distclean:", text)
        self.assertIn("clean:", text)
        self.assertIn("VERSION", text)
        self.assertIn("filter $(VERSION),$(VERSIONS)", text)
