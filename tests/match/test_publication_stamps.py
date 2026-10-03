"""Publication keeps proved receipts current without modifying shared generations."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.match import staging


class PublicationStampTests(unittest.TestCase):
    def test_refreshes_graph_and_receipts_after_inputs_without_changing_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous, generation = root / "old", root / "new"
            (previous / "obj/asm").mkdir(parents=True)
            old = previous / "obj/asm/data.built"
            old.write_bytes(b"receipt")
            os.utime(old, ns=(1, 1))
            generation.mkdir()
            (generation / "obj/src").mkdir(parents=True)
            (generation / "obj/asm").symlink_to(previous / "obj/asm", target_is_directory=True)
            obj = generation / "obj/src/alpha.o"
            obj.write_bytes(b"proved object")
            stamp = obj.with_suffix(".built")
            stamp.touch()
            (generation / "obj/src/shared.built").symlink_to(old)
            for name in (".split.mk", ".split"):
                (generation / name).write_text("graph")
                os.utime(generation / name, ns=(1, 1))
            with patch.object(staging, "independent_objects", wraps=staging.independent_objects) as detach:
                staging.publication_stamps(SimpleNamespace(), {"us": generation})
                detach.assert_called_once_with(generation)
            self.assertEqual(old.stat().st_mtime_ns, 1)
            self.assertFalse((generation / "obj/asm").is_symlink())
            self.assertEqual(obj.read_bytes(), b"proved object")
            for path in (stamp, generation / "obj/asm/data.built", generation / ".split.mk", generation / ".split"):
                self.assertGreater(path.stat().st_mtime_ns, 1)
            source = root / "source.c"
            source.write_text("later edit")
            self.assertGreaterEqual(source.stat().st_mtime_ns, stamp.stat().st_mtime_ns)

    def test_missing_receipts_are_not_invented_and_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            generation = Path(directory)
            with patch.object(staging, "independent_objects"):
                staging.publication_stamps(SimpleNamespace(), {"us": generation})
            self.assertEqual(list(generation.iterdir()), [])
            with (
                patch.object(staging, "independent_objects", side_effect=OSError("detach failed")),
                self.assertRaisesRegex(OSError, "detach failed"),
            ):
                staging.publication_stamps(SimpleNamespace(), {"us": generation})
