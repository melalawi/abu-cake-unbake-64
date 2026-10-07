"""Committed value layouts survive a stale disposable header catalogue."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake import process
from unbake.layout import header_context
from unbake.layout.header_context import Headers
from unbake.layout.structs_identity import identity

FIXTURE = Path(__file__).parents[1] / "fixtures/typedef_layout_closure/include"
PROVIDER = "common/types_d507c48987bb.h"
CONSUMER = "span_1000/code_80091A60.h"


class TypedefLayoutClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary).resolve()
        self.include = self.root / "include"
        shutil.copytree(FIXTURE, self.include)
        self.project = SimpleNamespace(root=self.root, include=(self.include,), build=self.root / "build")
        self.provider = self.include / PROVIDER
        self.consumer = self.include / CONSUMER
        self.manifest(self.consumer)

    def manifest(self, *headers):
        target = self.project.build / "layout/index.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "symbols": {},
                    "clusters": {},
                    "headers": {
                        path.relative_to(self.include).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in headers
                    },
                }
            )
        )

    def test_committed_include_closes_value_layout_once_without_tools(self):
        with (
            patch.object(header_context, "_parser", wraps=header_context._parser) as parse,
            patch.object(process.subprocess, "run", side_effect=AssertionError("layout lookup starts no tools")) as run,
        ):
            headers = Headers.read(self.project)
            reused = Headers.read(self.project)
        self.assertEqual(parse.call_count, 1)
        run.assert_not_called()
        self.assertEqual(reused.texts, headers.texts)
        self.assertEqual(list(headers.texts), [self.provider, self.consumer])
        box = headers.index.names["QueryBox"]
        record = headers.index.names["QueryRecord"]
        self.assertEqual((box.size, box.alignment), (18, 2))
        self.assertEqual((record.size, record.alignment), (40, 2))
        self.assertEqual([(field.offset, field.size) for field in record.fields], [(0, 18), (18, 22)])
        self.assertEqual(record.fields[0].fields, box.fields)
        self.assertEqual(headers.homes["QueryBox"], self.provider)
        self.assertEqual(headers.homes["struct QueryBox"], self.provider)
        self.assertIs(headers.types["QueryBox"][0], headers.types["struct QueryBox"])
        _, tagged = headers.parse("struct Tagged { struct QueryBox box; char tail[22]; };")
        self.assertEqual(identity(record, headers.index.names), identity(tagged[0], headers.index.names))

    def test_true_incomplete_missing_alias_and_conflicting_definition_refuse(self):
        from unbake.config import Held

        original = self.provider.read_text()
        cases = (
            (original.replace("struct QueryBox { short values[9]; };", ""), "missing aggregate definition"),
            (original.replace("typedef struct QueryBox QueryBox;", ""), "missing type layout"),
            (original + "struct QueryBox { int other; };\n", "duplicate definition"),
        )
        for source, reason in cases:
            with self.subTest(reason=reason):
                self.provider.write_text(source)
                with self.assertRaisesRegex(Held, reason):
                    Headers.read(self.project)

    def test_unreferenced_conflicting_orphan_is_excluded(self):
        orphan = self.include / "common/unused.h"
        orphan.write_text(
            "#ifndef UNBAKE_COMMON_UNUSED_H\n#define UNBAKE_COMMON_UNUSED_H\nstruct QueryBox { int other; };\n#endif\n"
        )
        headers = Headers.read(self.project)
        self.assertNotIn(orphan, headers.texts)
        self.assertEqual(headers.index.names["QueryBox"].size, 18)

    def test_unlisted_transitive_provider_and_relative_import_are_retained(self):
        wrapper = self.include / "common/wrapper.h"
        wrapper.write_text(
            "#ifndef UNBAKE_COMMON_WRAPPER_H\n#define UNBAKE_COMMON_WRAPPER_H\n"
            '#include "types_d507c48987bb.h"\n#endif\n'
        )
        self.consumer.write_text(self.consumer.read_text().replace(PROVIDER, "../common/wrapper.h"))
        headers = Headers.read(self.project)
        self.assertEqual(set(headers.texts), {self.consumer, self.provider, wrapper})
        self.assertEqual(headers.index.names["QueryRecord"].size, 40)

    def test_private_include_root_shadows_public_provider(self):
        private = self.root / "private"
        provider = private / PROVIDER
        provider.parent.mkdir(parents=True)
        provider.write_text(self.provider.read_text().replace("values[9]", "values[1]"))
        self.project.include = (private, self.include)
        self.project.work_include = (private,)
        self.consumer.write_text(self.consumer.read_text().replace(f'"{PROVIDER}"', f"<{PROVIDER}>"))
        headers = Headers.read(self.project)
        self.assertIn(provider, headers.texts)
        self.assertNotIn(self.provider, headers.texts)
        self.assertEqual(headers.index.names["QueryBox"].size, 2)
        self.assertEqual(headers.index.names["QueryRecord"].size, 24)

    def test_commented_import_does_not_admit_an_orphan(self):
        orphan = self.include / "common/unused.h"
        orphan.write_text(
            "#ifndef UNBAKE_COMMON_UNUSED_H\n#define UNBAKE_COMMON_UNUSED_H\nstruct QueryBox { int other; };\n#endif\n"
        )
        self.consumer.write_text(self.consumer.read_text() + '/*\n#include "common/unused.h"\n*/\n')
        headers = Headers.read(self.project)
        self.assertNotIn(orphan, headers.texts)
        self.assertEqual(headers.index.names["QueryRecord"].size, 40)
