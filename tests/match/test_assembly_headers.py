"""C dependency proofs retain assembler includes without hydrating assembly."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.match import incremental, staging


class AssemblyHeaderTests(unittest.TestCase):
    def test_standard_and_configured_roots_preserve_dependency_checks_and_fallback(self):
        for flags in ((), ("-Iasm/custom",), ("-I", "asm/custom")):
            for changed in (False, True):
                with self.subTest(flags=flags, changed=changed), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "original"
                    tree = root.parent / "staged"
                    for base in (root, tree):
                        (base / "src").mkdir(parents=True)
                        (base / "src/alpha.c").write_text("int alpha(void) {}\n")
                    header = root / "asm/us/include/nested/macro.inc"
                    header.parent.mkdir(parents=True)
                    header.write_bytes(b"macro")
                    custom = root / "asm/custom/helper.s"
                    custom.parent.mkdir(parents=True)
                    custom.write_bytes(b"custom include")
                    source = root / "asm/us/nonmatchings/alpha.s"
                    source.parent.mkdir(parents=True)
                    source.write_bytes(b"assembly")
                    generation = root / "generation"
                    obj = generation / "obj/src/alpha.o"
                    obj.parent.mkdir(parents=True)
                    obj.write_bytes(b"object")
                    obj.with_suffix(".built").write_bytes(b"proved")
                    obj.with_suffix(".d").write_text("object: src/alpha.c asm/us/include/nested/macro.inc\n")
                    obj.with_suffix(".inputs.json").write_text(
                        json.dumps(
                            {
                                "src/alpha.c": hashlib.sha256((root / "src/alpha.c").read_bytes()).hexdigest(),
                                "asm/us/include/nested/macro.inc": hashlib.sha256(header.read_bytes()).hexdigest(),
                            }
                        )
                    )
                    original = SimpleNamespace(
                        root=root,
                        asm=root / "asm",
                        src=root / "src",
                        versions=("us",),
                        compiler_for=lambda p: SimpleNamespace(id="cc"),
                    )
                    staged = SimpleNamespace(
                        root=tree,
                        asm=tree / "asm",
                        src=tree / "src",
                        compiler_for=lambda p: SimpleNamespace(id="cc"),
                        version=lambda v, root=root: SimpleNamespace(split=root / "split"),
                    )
                    (root / "split").write_text("")
                    with patch.object(incremental.extract, "unit_ranges", return_value={"alpha": {}}):
                        self.assertEqual(
                            incremental.changed_sources(original, staged, generation, "us"), [tree / "src/alpha.c"]
                        )
                    if changed:
                        header.write_bytes(b"changed macro")
                    recipe = SimpleNamespace(asflags=flags, sn64_asflags=())
                    with patch.object(staging.makefile, "recipe", return_value=recipe):
                        staging.copy_assembly_headers(original, staged)
                    copied = tree / header.relative_to(root)
                    self.assertEqual(copied.read_bytes(), header.read_bytes())
                    self.assertNotEqual(copied.stat().st_ino, header.stat().st_ino)
                    self.assertFalse((tree / source.relative_to(root)).exists())
                    self.assertEqual((tree / custom.relative_to(root)).exists(), bool(flags))
                    with patch.object(incremental.extract, "unit_ranges", return_value={"alpha": {}}):
                        self.assertEqual(
                            incremental.changed_sources(original, staged, generation, "us"),
                            [tree / "src/alpha.c"] if changed else [],
                        )
                    staging.copy_assembly(original, staged)
                    self.assertEqual((tree / source.relative_to(root)).read_bytes(), b"assembly")
                    copied.write_bytes(b"private edit")
                    self.assertEqual(header.read_bytes(), b"changed macro" if changed else b"macro")


class PublicationDependenciesTests(unittest.TestCase):
    def test_staged_dependencies_publish_locally_without_mutating_retained_inodes(self):
        import json
        import os
        from types import SimpleNamespace

        from unbake.match import staging

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "build/submit-proof/tree"
            generation = root / "build/us.new"
            obj = generation / "obj/src/unit.o"
            obj.parent.mkdir(parents=True)
            obj.write_bytes(b"proved")
            (generation / ".split.mk").write_text("C_OBJECTS := $(BUILD)/obj/src/unit.o\n")
            dependency = obj.with_suffix(".d")
            dependency.write_text(f"$(BUILD)/obj/src/unit.built: {tree}/src/unit.c {tree}/asm/us/include/macro.inc\n")
            previous = root / "old.d"
            os.link(dependency, previous)
            evidence = obj.with_suffix(".inputs.json")
            evidence.write_text(json.dumps({str(tree / "src/unit.c"): "digest"}))
            staging.publication_dependencies(SimpleNamespace(root=root), SimpleNamespace(root=tree), {"us": generation})
            self.assertEqual(
                dependency.read_text(), "$(BUILD)/obj/src/unit.built: src/unit.c asm/us/include/macro.inc\n"
            )
            self.assertIn("submit-proof/tree", previous.read_text())
            self.assertEqual(json.loads(evidence.read_text()), {"src/unit.c": "digest"})


class LinkIdentityTests(unittest.TestCase):
    def test_nonloaded_debug_changes_reuse_but_code_symbols_and_loaded_debug_do_not(self):
        import struct

        from tests.elf_fixture import write_object
        from unbake.objects.elf import Object

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def object_at(name, code=b"code", symbol="unit", debug=b"old", loaded=False):
                path = write_object(root / name, {".text": code, ".mdebug": debug}, [(symbol, ".text", 0, 4)])
                obj = Object(path)
                if not loaded:
                    struct.pack_into(">I", obj.data, obj.table + obj.section(".mdebug") * 40 + 8, 0)
                    path.write_bytes(obj.data)
                return path

            old = object_at("old.o")
            changed_debug = object_at("debug.o", debug=b"new debug with more source lines")
            self.assertEqual(staging.object_identity(old), staging.object_identity(changed_debug))
            for changed in (
                object_at("code.o", code=b"diff"),
                object_at("symbols.o", symbol="other"),
                object_at("loaded.o", loaded=True),
            ):
                self.assertNotEqual(staging.object_identity(old), staging.object_identity(changed))
