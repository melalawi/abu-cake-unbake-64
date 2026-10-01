"""Transactional bootstrap with executable fixture tools and byte comparison."""

import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project.test_bootstrap import policy
from unbake.layout import split
from unbake.project import config, fingerprint, header, init, makefile, rom
from unbake.project.config import Held
from unbake.report import progress as report

TOOLS = (
    r"""#!/usr/bin/env python3
import os
from pathlib import Path
import shutil
import sys
tool = Path(sys.argv[0]).name
arguments = sys.argv[1:]
root = Path.cwd()
with (Path(os.environ['FIXTURE_LOG'])).open('a') as log:
    log.write(tool + ' ' + ' '.join(arguments) + '\n')
if tool == 'git':
    if arguments[:2] == ['config', '--get']:
        if os.environ.get('FIXTURE_NO_AUTHOR'):
            sys.exit(1)
        print('Contributor' if arguments[2] == 'user.name' else 'contributor@example.org')
    elif arguments[0] == 'init':
        (root / '.git').mkdir()
    elif arguments[0] == 'add':
        (root / '.git' / 'staging-arguments').write_text('\n'.join(arguments))
    elif arguments[0] == 'commit':
        if os.environ.get('FIXTURE_COMMIT_FAIL'):
            sys.exit(1)
        (root / '.git' / 'first').write_text(arguments[-1])
elif tool == 'splat':
    if arguments[0] == 'create_config':
        (root / 'fixture.yaml').write_text("""
    + repr(
        "name: Fixture\noptions:\n  basename: fixture\n  platform: n64\n  comp"
        "iler: KMC\n  target_path: input.z64\n  base_path: .\nsegments:\n  - ["
        "0x0, header, header]\n  - [0x40, bin, boot]\n  - name: main\n    typ"
        "e: code\n    start: 0x1000\n    vram: 0x80001000\n    subsegments:\n "
        "     - [0x1000, asm]\n  - [0x1060, bin, tail]\n  - [0x1100]\n"
    )
    + r""")
    else:
        version = Path(arguments[1]).parent.name
        assembly = root / 'asm' / version / 'text.s'
        assembly.parent.mkdir(parents=True, exist_ok=True)
        text = '.section .text, "ax"\n'
        for index, name in enumerate(('alpha', 'beta', 'gamma')):
            offset = 0x1000 + index * 32
            text += 'glabel ' + name + '\n'
            for at in range(offset, offset + 32, 4):
                text += f'/* {at:06X} {at + 0x80000000:08X} 00801021 */ addu $v0, $a0, $zero\n'
        assembly.write_text(text)
elif tool == 'make':
    if 'extract' not in arguments:
        version = next(value.split('=', 1)[1] for value in arguments if value.startswith('VERSION='))
        generation = root / 'build' / (version + '.0')
        generation.mkdir(parents=True, exist_ok=True)
        link = root / 'build' / version
        if not link.is_symlink():
            link.symlink_to(generation.name, target_is_directory=True)
        output = link / ('example.' + version + '.z64')
        data = bytearray((root / ('baserom.' + version + '.z64')).read_bytes())
        if os.environ.get('FIXTURE_MISMATCH') or os.environ.get('FIXTURE_MISMATCH_VERSION') == version:
            data[0x1020] ^= 1
        output.write_bytes(data)
        bytecode = root / 'tools' / '__pycache__' / 'compile.cpython-311.pyc'
        bytecode.parent.mkdir(parents=True, exist_ok=True)
        bytecode.write_bytes(b'cached tool')
"""
)


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "result"
        self.inputs = self.root / "roms"
        self.inputs.mkdir()
        data = bytearray(0x101000)
        data[:4] = bytes.fromhex("80371240")
        data[0x20:0x34] = b"Example             "
        data[0x3B:0x40] = b"NEXE\0"
        for offset in range(0x1000, 0x1060, 4):
            struct.pack_into(">I", data, offset, 0x00801021)
        struct.pack_into(">II", data, 0x10, *header.checksum(data, "6102/7101"))
        (self.inputs / "example.z64").write_bytes(data)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("git", "make", "splat"):
            path = self.bin / tool
            path.write_text(TOOLS)
            path.chmod(0o755)
        self.log = self.root / "commands"
        self.policy = replace(policy(self.root), splat=self.bin / "splat")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(header, "RETAIL", {zlib.crc32(data[0x40:0x1000]): "6102/7101"}))
        self.stack.enter_context(
            patch.dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], FIXTURE_LOG=str(self.log))
        )
        self.stack.enter_context(patch("unbake.project.config.load_policy", return_value=self.policy))
        self.install = self.stack.enter_context(patch("unbake.project.toolchain.ensure"))
        self.stack.enter_context(
            patch(
                "unbake.project.setup.run",
                side_effect=lambda project, policy: (project.root / "Makefile").write_text("all:\n"),
            )
        )

    def forced(self) -> init.Forced:
        return init.Forced(compiler={"*": "gcc-2.7.2-kmc"}, name="example")

    def test_five_version_init_proves_every_rom_before_publishing(self) -> None:
        original = (self.inputs / "example.z64").read_bytes()
        (self.inputs / "example.z64").unlink()
        versions = ("us", "us-rev1", "eu", "eu-x", "de")
        for version, region, revision in (
            ("de", "D", 0),
            ("eu-x", "X", 0),
            ("eu", "P", 0),
            ("us-rev1", "E", 1),
            ("us", "E", 0),
        ):
            data = bytearray(original)
            data[0x3E:0x40] = bytes((ord(region), revision))
            (self.inputs / f"{version}.z64").write_bytes(data)
        forced = replace(self.forced(), version_names=dict(zip(versions, versions, strict=True)), names_from="us-rev1")
        inputs = sorted(self.inputs.iterdir())
        receipts = init.run(self.target, inputs, forced)
        self.assertEqual(config.load(self.target).versions, versions)
        for version in versions:
            with self.subTest(version=version):
                self.assertEqual(
                    (self.target / "build" / version / f"example.{version}.z64").read_bytes(),
                    (self.inputs / f"{version}.z64").read_bytes(),
                )
                self.assertTrue(any(f"VERSION {version} " in line and "byte-identical" in line for line in receipts))
        failed = self.root / "failed"
        with patch.dict(os.environ, FIXTURE_MISMATCH_VERSION="de"), self.assertRaisesRegex(Held, "VERSION de.*0x1020"):
            init.run(failed, inputs, forced)
        self.assertFalse(failed.exists())

    def test_fresh_init_committed_inputs_have_no_absolute_paths(self) -> None:
        def rendered(project: object, policy: object) -> None:
            for name, content in makefile.render(project).items():
                path = project.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

        with patch("unbake.project.setup.run", side_effect=rendered):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        paths = [
            path
            for path in self.target.rglob("*")
            if path.is_file()
            and path.parts[-2] != ".git"
            and not path.is_relative_to(self.target / "build")
            and not path.is_relative_to(self.target / "asm")
            and not path.name.startswith("baserom.")
            and "__pycache__" not in path.parts
        ]
        result = subprocess.run(
            ["grep", "-nE", r"[\"']/[A-Za-z0-9]|= */[A-Za-z0-9]", *map(str, paths)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_forced_fixture_publishes_only_after_extract_and_byte_proof_then_commits(self) -> None:
        before = (self.inputs / "example.z64").read_bytes()
        receipts = init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.assertEqual((self.target / "baserom.us.z64").read_bytes(), before)
        self.assertEqual((self.inputs / "example.z64").read_bytes(), before)
        self.assertIn("Initial all-asm split of Example (us)", (self.target / ".git" / "first").read_text())
        commands = self.log.read_text()
        self.assertLess(commands.index("make -j2 extract"), commands.index("make -j2 VERSION"))
        self.assertLess(commands.index("make -j2 VERSION"), commands.index("git init -b main"))
        self.assertTrue(any("byte-identical" in line for line in receipts))
        self.assertFalse(list(self.root.glob("result.init-*")))
        self.assertIn("[compilers.", (self.target / "config.toml").read_text())
        self.assertIn("alpha", (self.target / "versions/us/example.yaml").read_text())
        self.assertTrue((self.target / "tools/__pycache__/compile.cpython-311.pyc").is_file())
        ignored = (self.target / ".gitignore").read_text().splitlines()
        self.assertIn("__pycache__/", ignored)
        self.assertIn("*.py[cod]", ignored)
        staging = (self.target / ".git/staging-arguments").read_text().splitlines()
        for exclusion in (":(exclude)**/__pycache__/**", ":(exclude)*.pyc", ":(exclude)*.pyo"):
            with self.subTest(exclusion=exclusion):
                self.assertIn(exclusion, staging)

    def test_mismatch_names_version_and_first_offset_and_leaves_no_target(self) -> None:
        with patch.dict(os.environ, FIXTURE_MISMATCH="1"), self.assertRaisesRegex(Held, "VERSION us.*0x1020"):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob("result.init-*")))
        self.assertNotIn("git init", self.log.read_text())

    def test_fresh_init_ignores_every_configured_compiler(self) -> None:
        init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        project = config.load(self.target)
        content = (self.target / ".gitignore").read_text()
        for ident in project.compilers:
            self.assertIn(f"/tools/{ident}/\n", content)
        self.assertIn("/.splat/\n", content)

    def test_fresh_project_readme_supports_progress_rendering(self) -> None:
        init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        readme = (self.target / "README.md").read_text()
        self.assertTrue((self.target / "build/us").is_symlink())
        self.assertEqual((self.target / "build/us").readlink(), Path("us.0"))
        self.assertIn("| us (us, revision 0) |", readme)
        self.assertIn("bytes     [" + "░" * 20 + "]   0.00% (~0.00%)  0 of 96", readme)
        updated = report.render(
            readme,
            {
                "us": {
                    "version": 2,
                    "measures": {
                        "complete_code": 32,
                        "total_code": 96,
                        "matched_code_percent": 100 / 3,
                        "fuzzy_match_percent": 50,
                    },
                }
            },
        )
        self.assertIn("bytes     [██████▒▒▒▒░░░░░░░░░░]  33.33% (~50.00%)  32 of 96", updated)
        self.assertEqual(updated.split("## Building")[1], readme.split("## Building")[1])

    def test_empty_existing_target_is_preserved_after_commit_failure(self) -> None:
        self.target.mkdir()
        with patch.dict(os.environ, FIXTURE_COMMIT_FAIL="1"), self.assertRaisesRegex(Held, "git commit"):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.assertTrue(self.target.is_dir())
        self.assertFalse(list(self.target.iterdir()))

    def test_missing_author_refuses_before_splat_and_target_creation(self) -> None:
        with patch.dict(os.environ, FIXTURE_NO_AUTHOR="1"), self.assertRaisesRegex(Held, "user.name"):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.assertFalse(self.target.exists())
        self.assertNotIn("splat", self.log.read_text())

    def test_non_rom_duplicate_and_nonempty_target_are_refused(self) -> None:
        (self.inputs / "README").write_text("documentation")
        with self.assertRaisesRegex(Held, "README.*not an N64 ROM"):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        (self.inputs / "README").unlink()
        (self.inputs / "duplicate").write_bytes((self.inputs / "example.z64").read_bytes())
        with self.assertRaisesRegex(Held, "duplicate sha1"):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.target.mkdir()
        (self.target / "keep").write_text("keep")
        with self.assertRaisesRegex(Held, "non-empty"):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.assertEqual((self.target / "keep").read_text(), "keep")

    def test_missing_recipe_value_is_named(self) -> None:
        self.policy = replace(self.policy, mips_objcopy=None)
        with (
            patch("unbake.project.config.load_policy", return_value=self.policy),
            self.assertRaisesRegex(Held, "policy.mips_objcopy"),
        ):
            init.run(self.target, sorted(self.inputs.iterdir()), self.forced())
        self.assertFalse(self.target.exists())

    def test_forced_range_partitions_measured_functions_and_accepts_lowercase_hex(self) -> None:
        cartridge = rom.load(self.inputs / "example.z64")
        functions = tuple(
            split.Function("us", name, start, start + 32, start + 0x80000000, name, "asm", ())
            for name, start in (("alpha", 0x1000), ("beta", 0x1020), ("gamma", 0x1040))
        )
        regions = [fingerprint.Region(0x80001000, 0x80001060, "gcc", fingerprint.Counts(24, 0), "main", functions)]
        forced = init.Forced(compiler={"0x80001020-0x80001040": "ido-7.1"})
        specs = {"ido-7.1": SimpleNamespace(family="ido")}
        cut = init.forced_ranges(regions, cartridge, forced, specs)
        self.assertEqual(
            [(r.name, r.start, r.end) for r in cut],
            [
                ("main", 0x80001000, 0x80001020),
                ("ido", 0x80001020, 0x80001040),
                ("main_80001040", 0x80001040, 0x80001060),
            ],
        )
        self.assertEqual(init.forced_compiler(cut[1], forced, specs), "ido-7.1")
        with self.assertRaisesRegex(Held, "measured function boundaries"):
            init.forced_ranges(regions, cartridge, init.Forced(compiler={"0x80001024-0x80001040": "ido-7.1"}), specs)
