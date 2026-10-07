"""Kernel controls and stable locks share the owning IO boundary, never regular-file truncation."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import atomic
from unbake.config import Held
from unbake.project import write_hygiene


class AtomicBoundaryTests(TempCase):
    def test_control_refuses_a_regular_file_without_changing_it(self):
        path = self.root / "authored"
        path.write_bytes(b"preserved")
        with self.assertRaisesRegex(Held, "kernel control"):
            atomic.control(path, b"changed")
        self.assertEqual(path.read_bytes(), b"preserved")

    def test_kernel_control_uses_one_existing_inode_write_and_closes(self):
        with (
            patch.object(atomic.os, "open", return_value=17) as opened,
            patch.object(atomic.os, "stat", return_value=SimpleNamespace(st_dev=1)),
            patch.object(atomic.os, "fstat", return_value=SimpleNamespace(st_dev=1)),
            patch.object(atomic.os, "write", return_value=3) as written,
            patch.object(atomic.os, "close") as closed,
        ):
            path = Path("/sys/fs/cgroup/test/cgroup.procs")
            atomic.control(path, b"123")
        opened.assert_called_once_with(path, os.O_WRONLY)
        written.assert_called_once_with(17, b"123")
        closed.assert_called_once_with(17)

    def test_lock_keeps_the_same_inode_across_operations(self):
        path = self.root / "lock"
        with atomic.lock(path):
            inode = path.stat().st_ino
        with atomic.lock(path):
            self.assertEqual(path.stat().st_ino, inode)

    def test_hygiene_distinguishes_read_descriptors_and_compressed_reads_from_writes(self):
        for source in (
            'import gzip\ngzip.open(source, "rb")',
            "import os\nos.open(path, os.O_RDONLY | os.O_CLOEXEC)",
            "import os as system\nsystem.open(path, system.O_RDONLY)",
        ):
            with self.subTest(source=source):
                self.assertEqual(write_hygiene.violations(Path("example.py"), source), [])
        for source in (
            'import gzip\ngzip.open(source, "wb")',
            "import os\nos.open(path, os.O_WRONLY)",
            "import os\nos.open(path, os.O_RDONLY | unknown_flags)",
            'path.write_bytes(b"authored")',
        ):
            with self.subTest(source=source):
                self.assertTrue(write_hygiene.violations(Path("example.py"), source))
