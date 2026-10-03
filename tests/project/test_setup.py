import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.project.makefile_fixture import WORK, fixture
from tests.project.test_config import write_policy
from unbake.cli.main import make_parser
from unbake.cli.setup import run as setup_command
from unbake.project import config, setup
from unbake.report import progress, readme_layout


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name).resolve()
        policy_path = write_policy(self.root)
        patch.dict(os.environ, UNBAKE_POLICY=str(policy_path)).start()
        self.project, self.policy = fixture(self.root, case=self)

    def test_verified_setup_writes_real_sha1_and_helpers(self) -> None:
        receipt = setup.run(self.project, self.policy)
        self.assertEqual(len(receipt), 1)
        self.assertTrue(receipt[0].startswith("OK(setup): us:"))
        expected = hashlib.sha1(b"ABC").hexdigest()
        self.assertEqual((self.root / "versions/us/game.sha1").read_text(), expected + "  build/us/game.us.z64\n")
        self.assertEqual((self.root / "versions/us/baserom.sha1").read_text(), expected + "  roms/baserom.us.z64\n")
        self.assertTrue((self.root / "tools/extract.py").is_file())
        self.assertTrue((self.root / "CONTRIBUTING.md").is_file())
        contributing = (self.root / "CONTRIBUTING.md").read_text()
        self.assertNotIn("@", contributing)
        self.assertIn("every version", contributing)

    def test_bad_rom_refuses_before_rendered_files_are_written(self) -> None:
        (self.root / "roms/baserom.us.z64").write_bytes(b"bad")
        with self.assertRaisesRegex(config.Held, r"baserom_sha1"):
            setup.run(self.project, self.policy)
        self.assertFalse((self.root / "Makefile").exists())

    def test_unchanged_setup_preserves_generated_timestamps(self) -> None:
        setup.run(self.project, self.policy)
        paths = [self.root / "Makefile", self.root / "tools/compiler.sha256", self.root / "tools/build.json"]
        before = [path.stat().st_mtime_ns for path in paths]
        setup.run(self.project, self.policy)
        self.assertEqual(before, [path.stat().st_mtime_ns for path in paths])

    def test_real_game_readmes_preserve_every_owner_byte_across_setup_and_report(self) -> None:
        patch("unbake.project.hygiene.subprocess.run", return_value=Mock(returncode=0, stdout=b"")).start()
        references = Path(__file__).parents[1] / "report"
        # Independently mask only generated bar art, percentages and counters.
        figures = re.compile(
            rb"\[(?:[#\-]|\xe2\x96[\x88\x92\x91]){20}\] +[0-9]+\.[0-9]+%"
            rb"(?: \(~[0-9]+\.[0-9]+%\))? +[0-9,]+ of [0-9,]+"
        )
        mask_identity = re.compile(rb"SHA256 `[0-9a-f]+`")
        readme = self.root / "README.md"
        for game in ("BattleTanx", "RageWars"):
            head = (references / "readme" / f"{game}.md").read_bytes()
            reports = json.loads((references / f"{game.lower()}-measures.json").read_bytes())
            reports = {name.replace("eu-mul", "eu-x"): doc for name, doc in reports.items()}
            labels = re.findall(r"^\| ([\w-]+) \(([^,]+), ([^)]+)\)\. (.*?) SHA256", head.decode(), re.MULTILINE)
            versions = tuple(row[0] for row in labels)
            project = replace(
                self.project,
                versions=versions,
                version_map={
                    name: replace(
                        self.project.version("us"),
                        name=name,
                        cartridge_id=ident,
                        region=region,
                        description=description,
                    )
                    for name, ident, region, description in labels
                },
            )
            # Exercise both the exact HEAD snapshot and live owner edits with CRLF.
            edited = (
                head.replace(b"## Progress\n\n", b"## Progress\n\nOwner progress notes: caf\xc3\xa9.\n\n").replace(
                    b"\n", b"\r\n"
                )
                + b"Owner footer: legacy byte \xff, without a final newline."
            )
            for variant, original in (("HEAD", head), ("owner CRLF", edited)):
                with self.subTest(game=game, variant=variant):
                    readme.write_bytes(original)
                    setup.run(project, self.policy)
                    self.assertEqual(readme.read_bytes(), original)
                    progress.write(project, self.policy, reports=reports)
                    rendered = readme.read_bytes()
                    progress.write(project, self.policy, reports=reports)
                    self.assertNotEqual(rendered, original)
                    before, _, after = readme_layout.section(original.decode(errors="surrogateescape"))
                    new_before, _, new_after = readme_layout.section(rendered.decode(errors="surrogateescape"))
                    self.assertEqual((new_before, new_after), (before, after))
                    if variant == "HEAD":
                        # Fixture ROM bytes differ from the real cartridge identity.
                        def owner_content(content: bytes) -> tuple[str, list[str], str]:
                            masked = mask_identity.sub(b"SHA256 <identity>", figures.sub(b"<generated>", content))
                            prefix, body, suffix = readme_layout.section(masked.decode(errors="surrogateescape"))
                            blocks = body.strip().split("\n\n")
                            if blocks[0].startswith("<pre>"):
                                summary = blocks[0][len("<pre>") : -len("</pre>")]
                                blocks[0] = "<br>".join(sorted(summary.split("<br>")))
                            return prefix, sorted(blocks), suffix

                        self.assertEqual(owner_content(rendered), owner_content(original))
                    self.assertEqual(readme.read_bytes(), rendered)
                    self.assertIn(b"## Notes", rendered)
                    self.assertIn(b"### Workflow", rendered)
                    self.assertIn(b"### Personal Thoughts", rendered)

    def test_bad_compiler_and_missing_pin_refused(self) -> None:
        (self.root / "tools/fixture/cc").write_text("wrong")
        with self.assertRaisesRegex(config.Held, "pins.cc: missing"):
            setup.run(self.project, self.policy)
        (self.root / "tools/fixture/cc").unlink()
        with self.assertRaisesRegex(config.Held, "fixture/cc"):
            setup.verify_compiler(self.project)

    def test_yaml_comments_refused_but_quoted_hash_allowed(self) -> None:
        split = self.project.version("us").split
        split.write_text(split.read_text() + "\n# prose\n")
        with self.assertRaisesRegex(config.Held, "YAML comment"):
            setup.run(self.project, self.policy)
        split.write_text('name: "hash#inside"\noptions:\n  base_path: .\nsegments: []\n')
        self.assertEqual(len(setup.run(self.project, self.policy)), 1)

    def test_sn64_manifest_covers_every_pipeline_executable(self) -> None:
        project, policy = fixture(self.root, "sn64", case=self)
        setup.run(project, policy)
        self.assertTrue((self.root / "tools/sn64_cc.py").is_file())
        self.assertIn("from abumasn64.assemble import assemble", (self.root / "tools/compile.py").read_text())

    def test_setup_retires_only_manifest_owned_helpers_and_compiler_files(self) -> None:
        stale_helper = self.root / "tools/retired.py"
        stale_compiler = self.root / "tools/fixture/retired"
        user_file = self.root / "tools/user_notes.py"
        stale_helper.write_bytes(b"obsolete helper")
        stale_compiler.write_bytes(b"obsolete executable")
        user_file.write_bytes(b"user content")
        manifest = self.root / "tools/compiler.sha256"
        with manifest.open("a") as output:
            for path in (stale_helper, stale_compiler):
                output.write(
                    hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(self.root)) + "\n"
                )
        setup.run(self.project, self.policy)
        self.assertFalse(stale_helper.exists())
        self.assertFalse(stale_compiler.exists())
        self.assertEqual(user_file.read_bytes(), b"user content")
        self.assertNotIn("retired", manifest.read_text())
        self.assertTrue((self.root / "tools/as").is_file())

    def test_supply_restores_every_version_by_sha1(self) -> None:
        setup.run(self.project, self.policy)
        other = replace(
            self.project.version("us"),
            name="eu",
            baserom=self.root / "other.z64",
            baserom_sha1=hashlib.sha1(b"XYZ").hexdigest(),
        )
        project = replace(
            self.project, versions=("us", "eu"), version_map={"us": self.project.version("us"), "eu": other}
        )
        source = self.root / "supply/nested"
        source.mkdir(parents=True)
        project.version("us").baserom.unlink()
        (source / "unexpected-name").write_bytes(b"ABC")
        with self.assertRaisesRegex(config.Held, "version.eu.*missing supplied SHA-1"):
            setup.restore_roms(project, source.parent)
        self.assertFalse(project.version("us").baserom.exists())
        (source / "another-name").write_bytes(b"XYZ")
        setup.restore_roms(project, source.parent)
        self.assertEqual(project.version("us").baserom.read_bytes(), b"ABC")
        self.assertEqual(other.baserom.read_bytes(), b"XYZ")
        help_text = make_parser().format_help()
        self.assertIn("setup", help_text)
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
            make_parser().parse_args(["setup", "--help"])
        self.assertIn("--supply", output.getvalue())

    def test_compiler_status_refuses_failed_install_and_labels_optional(self) -> None:
        from unbake.project import toolchain

        setup.run(self.project, self.policy)
        spec = toolchain.specification("fixture")
        optional = replace(spec, id="optional")
        with patch.object(toolchain, "registry", return_value={"fixture": spec, "optional": optional}):
            for broken in (False, True):
                with self.subTest(broken=broken):
                    if broken:
                        (self.policy.cache_root / "compilers/fixture/cc").unlink()
                    with (
                        redirect_stdout(io.StringIO()) as output,
                        patch.object(config, "load_policy", return_value=self.policy),
                    ):
                        status = int(setup_command(make_parser().parse_args(["setup", "--compilers"]), self.project))
                    self.assertEqual(status, int(broken))
                    self.assertIn("optional: not installed", output.getvalue())
                    if broken:
                        self.assertIn("HELD(setup): fixture:", output.getvalue())
                        self.assertNotIn("OK(setup): fixture", output.getvalue())
