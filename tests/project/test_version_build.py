"""Explicit naming facts and all-version make dispatch on tiny fixtures."""

import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project import config


class NamingConfigTests(unittest.TestCase):
    def test_names_from_is_required_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
        self.root = Path(self.temporary.name)
        project, _ = fixture(self.root)
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

    def make(self, *arguments: Any) -> Any:
        return subprocess.run(
            ["make", "-j2", *arguments], cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

    def test_plain_make_and_check_build_all_versions(self) -> None:
        for goal in ((), ("check",)):
            with self.subTest(goal=goal):
                result = self.make(*goal)
                self.assertEqual(result.returncode, 0, result.stdout)
                for version in self.project.versions:
                    self.assertEqual((self.root / f"build/{version}/game.{version}.z64").read_bytes(), b"ABC")
                result = self.make("clean")
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.root / "build/us").exists())
                self.assertFalse((self.root / "build/eu-x").exists())

    def test_distclean_removes_all_build_and_assembly_outputs(self) -> None:
        for directory in ("build/us.1", "build/eu-x.nonmatching", "asm/eu-x"):
            (self.root / directory).mkdir(parents=True)
        result = self.make("distclean")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "build").exists())
        self.assertFalse((self.root / "asm").exists())

    def test_selected_version_and_check_cannot_disable_verification(self) -> None:
        result = self.make("VERSION=eu-x", "COMPARE=0")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "build/us").exists())
        (self.root / "build/eu-x/game.eu-x.z64").write_bytes(b"bad")
        result = self.make("VERSION=eu-x", "check", "COMPARE=0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout)

    def test_unknown_version_and_ambiguous_build_are_named(self) -> None:
        for arguments, field in (
            (("VERSION=missing",), "VERSION=missing"),
            (("BUILD=other",), "BUILD requires VERSION"),
        ):
            with self.subTest(arguments=arguments):
                result = self.make(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(field, result.stdout)
