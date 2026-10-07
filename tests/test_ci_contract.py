"""The canonical repository check accepts only the current explicit host contract."""

import hashlib
import io
import os
import shutil
import sys
from contextlib import chdir, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import toml

from tests.kit import TempCase, host_values
from unbake.config import load_host


class CanonicalHostGeneratorTests(TempCase):
    def setUp(self):
        super().setUp()
        repository = Path(__file__).resolve().parents[1]
        (self.root / "ci").mkdir()
        shutil.copy2(repository / "ci/check", self.root / "ci/check")
        shutil.copy2(repository / "ci/policy", self.root / "ci/policy")
        (self.root / "src").symlink_to(repository / "src", target_is_directory=True)
        (self.root / ".venv").symlink_to(repository / ".venv", target_is_directory=True)
        (self.root / "tmp").mkdir()
        self.host = self.root / "host.toml"
        self.host.write_text(toml.dumps(host_values(self.root)))
        self.environment = dict(os.environ, TMPDIR=str(self.root / "tmp"), UNBAKE_CONFIG=str(self.host))
        for name in ("test", "lint", "hygiene"):
            path = self.root / "bin" / name
            path.write_text(f'#!/bin/sh\nprintf "{name}\\n" >> "$TMPDIR/checks"\nexit 0\n')
            path.chmod(0o755)

    def generator(self):
        text = (self.root / "ci/policy").read_text().split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]
        return compile(text, "ci/policy", "exec")

    def test_generator_writes_current_host_and_isolated_native_path(self):
        scratch = self.root / "policy"
        values = toml.loads(self.host.read_text())
        which = shutil.which
        tools = {
            "mips-linux-gnu-" + name: values["tools"]["mips_" + name]
            for name in ("as", "ld", "objcopy", "objdump", "readelf")
        }
        tools.update({name: values["tools"][name] for name in ("make", "cpp", "n64link", "m2c", "splat")})
        archive = b"downloaded pinned fixture archive"
        digest = "2e4e0d991e4258df332c19d8dcf372212fe27f1b18e777d9e1eb25fc2c64a404"
        # Download and checksum are external seams; parsing and validating the generated host are real.
        with (
            chdir(self.root),
            redirect_stdout(io.StringIO()) as output,
            patch.object(sys, "argv", ["ci/policy", str(scratch)]),
            patch.object(shutil, "which", side_effect=lambda name: tools.get(name) or which(name)),
            patch("urllib.request.urlopen", return_value=io.BytesIO(archive)) as download,
            patch.object(hashlib, "sha256") as checksum,
        ):
            checksum.return_value.hexdigest.return_value = digest
            exec(self.generator(), {"__name__": "__main__"})
        checksum.assert_called_once_with(archive)
        self.assertIn("059609d4aec73eb0650726772954e1ad575825f8", download.call_args.args[0])
        self.assertEqual(output.getvalue().strip(), str(scratch / "unbake.toml"))
        host = load_host(scratch / "unbake.toml", None, "check")
        host.require_command("check")
        self.assertEqual(tuple(map(str, host.tool_path)), (str(scratch / "toolbin"),))
        self.assertFalse((scratch / "toolbin/python").exists())
        self.assertEqual((scratch / "toolbin/n64link").resolve(), Path(values["tools"]["n64link"]))

    def test_generator_refuses_missing_native_tool_before_download(self):
        which = shutil.which
        with (
            chdir(self.root),
            patch.object(sys, "argv", ["ci/policy", str(self.root / "policy")]),
            patch.object(shutil, "which", side_effect=lambda name: None if name == "n64link" else which("sh")),
            patch("urllib.request.urlopen") as download,
            self.assertRaisesRegex(SystemExit, "missing tool: n64link"),
        ):
            exec(self.generator(), {"__name__": "__main__"})
        download.assert_not_called()
