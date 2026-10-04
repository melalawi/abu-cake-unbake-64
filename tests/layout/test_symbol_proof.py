"""Retained proof checks header freshness once for all objects."""

import hashlib
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.layout import symbol_proof


class ProofAvailabilityTests(unittest.TestCase):
    def test_freshness_table_and_linear_header_stats(self):
        for changed in (None, "source", "header"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                src, include, generation = root / "src", root / "include", root / "build"
                for path in (src, include, generation / "obj/src"):
                    path.mkdir(parents=True)
                split = root / "split.yaml"
                split.write_text("")
                (generation / "unit-ranges.json").write_text("{}")
                (generation / "symbol-addresses.txt").write_text("")
                (generation / "game.one.z64").write_bytes(b"rom")
                (generation / ".split.mk").write_text("C_OBJECTS := $(BUILD)/obj/src/a.o $(BUILD)/obj/src/b.o\n")
                headers = [include / f"{name}.h" for name in ("a", "b")]
                for header in headers:
                    header.write_text("typedef int scalar;\n")
                    os.utime(header, ns=(100, 300 if changed == "header" else 100))
                for name in ("a", "b"):
                    source = src / f"{name}.c"
                    source.write_text(f"void {name}(void) {{}}\n")
                    os.utime(source, ns=(100, 300 if changed == "source" else 100))
                    receipt = generation / f"obj/src/{name}.built"
                    receipt.write_text("")
                    os.utime(receipt, ns=(200, 200))
                version = SimpleNamespace(
                    split=split, symbols=root / "symbols", baserom_sha1=hashlib.sha1(b"rom").hexdigest()
                )
                project = SimpleNamespace(
                    src=src,
                    include=(include,),
                    versions=("one",),
                    name="game",
                    version=lambda v, configured=version: configured,
                    build_link=lambda v, current=generation: current,
                )
                counts = Counter()
                stat = Path.stat

                def measured(path, *args, _headers=tuple(headers), _counts=counts, _stat=stat, **kwargs):
                    if path in _headers:
                        _counts[path] += 1
                    return _stat(path, *args, **kwargs)

                with (
                    patch(
                        "unbake.layout.port.functions",
                        return_value=[SimpleNamespace(path=n, kind="c") for n in ("a", "b")],
                    ),
                    patch("unbake.layout.split.symbols", return_value=("", {})),
                    patch("unbake.project_tools.extract.unit_ranges", return_value={}),
                    patch.object(Path, "stat", autospec=True, side_effect=measured),
                ):
                    self.assertEqual(symbol_proof.available(project, {}), changed is None)
                self.assertEqual(counts, Counter({header: 1 for header in headers}))
