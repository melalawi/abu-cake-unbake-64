"""Publication keeps proved receipts current without modifying shared generations."""

import hashlib
import json
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
                (generation / name).write_text(
                    "C_OBJECTS := $(BUILD)/obj/src/alpha.o\nASM_OBJECTS := $(BUILD)/obj/asm/data.o\n"
                    if name == ".split.mk"
                    else "graph"
                )
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


class PublicationDependencyTests(unittest.TestCase):
    def test_removed_generated_prerequisite_is_rebound_and_receipt_invalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = SimpleNamespace(root=root / "project", include=(root / "project/include",))
            staged = SimpleNamespace(root=project.root / "build/submit/tree")
            generation = root / "generation"
            obj = generation / "obj/src/alpha.o"
            obj.parent.mkdir(parents=True)
            obj.write_bytes(b"retained")
            (generation / ".split.mk").write_text("C_OBJECTS := $(BUILD)/obj/src/alpha.o\n")
            removed = project.include[0] / "common/old.h"
            live = project.include[0] / "owners/new.h"
            live.parent.mkdir(parents=True)
            live.write_text("struct View {int value;};")
            dep = obj.with_suffix(".d")
            dep.write_text(
                "$(BUILD)/obj/src/alpha.built: src/alpha.c \\\n"
                + str(staged.root / "include/common/old.h")
                + " include/authored.h\n"
                + "another: "
                + str(removed)
                + "\n"
            )
            obj.with_suffix(".built").touch()
            obj.with_suffix(".inputs.json").write_text(json.dumps({str(removed): "old"}))
            with patch.object(staging.declaration_index, "headers", return_value={live}):
                stale = staging.publication_dependencies(project, staged, {"us": generation}, previous={removed})
                staging.publication_stamps(project, {"us": generation}, stale=stale)
            text = dep.read_text()
            self.assertNotIn("old.h", text)
            self.assertNotIn(str(staged.root), text)
            self.assertIn("src/alpha.c", text)
            self.assertIn("include/authored.h", text)
            self.assertEqual(text.count("include/owners/new.h"), 2)
            self.assertEqual(obj.with_suffix(".built").stat().st_mtime_ns, 1)
            self.assertFalse(obj.with_suffix(".inputs.json").exists())
            self.assertEqual(obj.read_bytes(), b"retained")

    def test_same_project_coordinates_preserve_parent_relative_includes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = SimpleNamespace(root=root)
            obj = root / "obj/src/alpha.o"
            obj.parent.mkdir(parents=True)
            (root / ".split.mk").write_text("C_OBJECTS := $(BUILD)/obj/src/alpha.o\n")
            obj.with_suffix(".d").write_text("target: include/owner/../types.h\n")
            staging.publication_dependencies(project, project, {"us": root})
            self.assertEqual(obj.with_suffix(".d").read_text(), "target: include/owner/../types.h\n")

    def test_coordinate_rebinding_keeps_unchanged_dependency_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = SimpleNamespace(root=root / "project")
            staged = SimpleNamespace(root=project.root / "build/submit/tree")
            generation = root / "generation"
            obj = generation / "obj/src/alpha.o"
            obj.parent.mkdir(parents=True)
            (generation / ".split.mk").write_text("C_OBJECTS := $(BUILD)/obj/src/alpha.o\n")
            source = staged.root / "src/alpha.c"
            digest = hashlib.sha256(b"source").hexdigest()
            obj.with_suffix(".d").write_text("target: " + str(source) + "\n")
            obj.with_suffix(".inputs.json").write_text(json.dumps({str(source): digest}))
            obj.with_suffix(".built").touch()
            staging.publication_dependencies(project, staged, {"us": generation})
            self.assertEqual(json.loads(obj.with_suffix(".inputs.json").read_text()), {"src/alpha.c": digest})
            self.assertTrue(obj.with_suffix(".built").exists())
