"""SN64 registry downloads only its pinned native compiler from the public bundle."""

import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from unbake.project import toolchain
from unbake.project.config import Compiler, Policy, Project


class Sn64DownloadTests(unittest.TestCase):
    def test_public_bundle_installs_cc1_without_supplied_files_or_bundled_drivers(self) -> None:
        spec = toolchain.specification("gcc-2.8.1-sn64")
        self.assertEqual(spec.source, "download")
        self.assertEqual(spec.cc, "cc1")
        self.assertEqual(spec.as_, "policy:mips_as")
        self.assertEqual(len(spec.downloads), 1)
        self.assertEqual(spec.downloads[0].files, ("cc1",))
        self.assertEqual(set(spec.pins), {"cc1"})
        content = b"native compiler fixture"
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            for name, data in {"cc1": content, "gcc": b"bundled driver", "cpp": b"bundled cpp"}.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        payload = archive_bytes.getvalue()
        pin = hashlib.sha256(content).hexdigest()
        entry = replace(spec.downloads[0], sha256=hashlib.sha256(payload).hexdigest())
        measured = replace(spec, pins={"cc1": pin}, downloads=(entry,))
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            root = Path(directory)
            (root / "config.toml").write_text(f'[compilers."{spec.id}"]\n')
            tools = root / "tools"
            compiler = Compiler(
                spec.id, spec.kind, tools / spec.id / spec.cc, Path(spec.as_), spec.cflags, tools / "compiler.sha256"
            )
            project = cast(Project, SimpleNamespace(root=root, tools=tools, compilers={spec.id: compiler}))
            policy = cast(Policy, SimpleNamespace(cache_root=root / "cache"))
            with (
                patch.object(toolchain, "registry", return_value={spec.id: measured}),
                patch("urllib.request.urlopen", return_value=io.BytesIO(payload)) as fetch,
            ):
                manifest = toolchain.ensure(project, policy)
            fetch.assert_called_once_with(entry.url, timeout=60)
            installed = tools / spec.id / "cc1"
            self.assertEqual(installed.read_bytes(), content)
            self.assertFalse(installed.is_symlink())
            self.assertTrue(os.access(installed, os.X_OK))
            self.assertEqual({path.name for path in installed.parent.iterdir()}, {"cc1"})
            self.assertEqual(manifest.read_text(), f"{pin}  tools/{spec.id}/cc1\n")
