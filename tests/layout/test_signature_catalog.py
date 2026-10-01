"""Pinned catalogs preserve relocation masks and fail closed on malformed inputs."""

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

from unbake.layout import boundary_signatures, signature_catalog
from unbake.project.config import Held


class SignatureCatalogTests(unittest.TestCase):
    def test_pinned_builder_matches_only_relocation_bits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notice = root / "LICENSE"
            notice.write_text("Permission is hereby granted, free of charge")
            source, output = root / "input.json", root / "catalog.json"
            original = struct.pack(">4I", 0x3C080000, 0x25080000, 0x0C000000, 0x03E00008)
            data = json.dumps(
                [
                    [
                        "osFixture",
                        16,
                        zlib.crc32(original[:8]),
                        zlib.crc32(original),
                        [["hi16", "data", [0]], ["lo16", "data", [4]], ["targ26", "call", [8]]],
                    ]
                ]
            ).encode()
            source.write_bytes(data)
            kwargs: dict[str, Any] = dict(
                source_url="https://example.org/sdk",
                commit="a" * 40,
                sha256=hashlib.sha256(data).hexdigest(),
                license_file=notice,
            )
            signature_catalog.build(source, output, **kwargs)
            first = output.read_bytes()
            signature_catalog.build(source, output, **kwargs)
            self.assertEqual(first, output.read_bytes())
            self.assertEqual(json.loads(first)["provenance"]["sha256"], kwargs["sha256"])
            catalog = boundary_signatures.load(output)
            for words, expected in [
                ((0x3C088001, 0x25088000, 0x0C123456, 0x03E00008), True),
                ((0x3C098001, 0x25088000, 0x0C123456, 0x03E00008), False),
                ((0x3C088001, 0x25088000, 0x08123456, 0x03E00008), False),
                ((0x3C088001, 0x25088000, 0x0C123456, 0), False),
            ]:
                with self.subTest(words=words):
                    self.assertEqual(
                        bool(boundary_signatures.matches(struct.pack(">4I", *words), 0, 16, catalog)), expected
                    )
            with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(Held, "UNBAKE_BOUNDARY_SIGNATURES"):
                boundary_signatures.configured()

    def test_builder_refuses_bad_pins_relocations_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, notice = root / "input", root / "output", root / "license"
            notice.write_text("Permission is hereby granted, free of charge")
            for change, relocation, error in [
                ({"commit": "main"}, [], "commit"),
                ({"sha256": "0" * 64}, [], "sha256"),
                ({"source_url": "file://sdk"}, [], "source-url"),
                ({}, [["gp16", "x", [0]]], "gp16"),
                ({}, [["lo16", "x", [16]]], "offset 16"),
                ({}, [["hi16", "x", [1]]], "offset 1"),
            ]:
                data = json.dumps([["osFixture", 16, 0, 0, relocation]]).encode()
                source.write_bytes(data)
                kwargs: dict[str, Any] = dict(
                    source_url="https://example.org/sdk",
                    commit="a" * 40,
                    sha256=hashlib.sha256(data).hexdigest(),
                    license_file=notice,
                )
                kwargs.update(change)
                with self.subTest(error=error), self.assertRaisesRegex(Held, error):
                    signature_catalog.build(source, output, **kwargs)
                self.assertFalse(output.exists())
            source.write_text(json.dumps([["sdk", 8, 0, 0, []]]))
            target = root / "target"
            target.write_text("preserve")
            output.symlink_to(target)
            with self.assertRaisesRegex(Held, "symlink"):
                signature_catalog.build(
                    source,
                    output,
                    source_url="https://example.org/sdk",
                    commit="a" * 40,
                    sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    license_file=notice,
                )
            self.assertEqual(target.read_text(), "preserve")

    def test_crc_ambiguity_supplies_no_identity_evidence(self) -> None:
        data = struct.pack(">2I", 0x03E00008, 0)
        signatures = tuple(
            boundary_signatures.CRCSignature(name, "fixture", (), (0, 0), 8, zlib.crc32(data), zlib.crc32(data))
            for name in ("dummy", "other")
        )
        self.assertFalse(boundary_signatures.matches(data, 0, 8, signatures))
