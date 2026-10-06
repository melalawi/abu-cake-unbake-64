"""Public contracts found by the complete independent source review."""

import io
import json
import struct
import tomllib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.compilers import propose
from unbake.config import Held
from unbake.cycle.events import Emitter, SchemaError
from unbake.decomp import original_asm, similar
from unbake.layout import split
from unbake.project import setup_config
from unbake.work import explain


class PublicReviewTests(ProjectCase):
    versions = ("us",)

    def test_creative_event_preserves_scored_and_skipped_methods_and_rejects_bad_scores(self):
        stream = io.StringIO()
        emitter = Emitter(stream)
        fields = dict(
            function="alpha",
            best_percent=50.0,
            methods={"order": 50.0, "permute": "skipped: no provider"},
            trouble="TROUBLE.md",
        )
        emitter.emit("fn.creative", **fields)
        self.assertEqual(json.loads(stream.getvalue())["methods"], fields["methods"])
        for invalid in ({"order": True}, {"order": 101.0}, {"order": float("nan")}, {"order": "success"}, ["order"]):
            with self.subTest(invalid=invalid), self.assertRaises(SchemaError):
                emitter.emit("fn.creative", **{**fields, "methods": invalid})
        self.assertEqual(emitter.seq, 1)

    def test_original_proof_uses_every_effective_compiler_shape(self):
        row = split.functions(self.project, "us")[0]
        native_only = struct.pack(">3I", 0x40846000, 0x03E00008, 0)
        self.assertEqual(original_asm.prove(self.project, row, native_only).rule, "cop0")
        isa3 = struct.pack(">3I", 0x0004103C, 0x03E00008, 0x0002103F)
        self.assertEqual(original_asm.prove(self.project, row, isa3).rule, "isa")
        project = replace(self.project, unit_flags={"beta": ("-mips3",)})
        with self.assertRaisesRegex(Held, "original.not_original"):
            original_asm.prove(project, row, isa3)

    def test_ready_refresh_keeps_explicit_unit_compiler_and_flags(self):
        config = (
            (self.project.root / "config.toml")
            .read_text()
            .replace("[units]", '[units]\nalpha = { compiler = "ido-7.1", flags = ["-O1", "-DLOCAL=1"] }')
        )
        rendered = setup_config.canonical(config)
        self.assertEqual(
            tomllib.loads(rendered)["units"]["alpha"], {"compiler": "ido-7.1", "flags": ["-O1", "-DLOCAL=1"]}
        )
        self.assertEqual(setup_config.canonical(rendered), rendered)

    def test_proposal_snapshot_pins_existing_evidence_owners(self):
        first = propose._inputs(self.project, {}, self.host, {}, layout_sha256="fixture-layout")
        self.assertEqual(len(first["evidence_engine"]), 64)
        self.assertEqual(propose._inputs(self.project, {}, self.host, {}, layout_sha256="fixture-layout"), first)

    def test_similar_explanation_is_a_json_document_with_a_source_path(self):
        source = self.project.src / "beta.c"
        example = SimpleNamespace(function="beta", distance=0.1, edit_distance=1, source=source)
        with (
            patch.object(similar, "retrieve", return_value=[example]),
            patch.object(explain.extract, "directory", return_value=self.project.root),
        ):
            report = explain.explain(self.project, self.host, "alpha", ("similar",))
        document = json.loads(json.dumps(report.document()))
        self.assertEqual(document["sections"]["similar"][0]["source"], str(source))
