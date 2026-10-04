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
