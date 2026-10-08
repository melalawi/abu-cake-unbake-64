"""Draft storage headers rebuilt at landing, named proof gaps, and cached pooled declaration scans."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from unbake import cdecl, pool
from unbake.config import Held
from unbake.decomp import field_access
from unbake.typemap import namespace
from unbake.work.score import Measurement


def project(root: Path) -> SimpleNamespace:
    return SimpleNamespace(work=root / "build/work", work_include=(), include=(root / "include",), cache=root / "cache")


class DraftFieldsRestore(unittest.TestCase):
    FUNCTION = "func_80266810_de"

    def source(self, *views: tuple[int, str, int, int]) -> tuple[str, list[str]]:
        tags, texts = [], []
        for offset, type_name, width, alignment in views:
            _tag, access, text = field_access.storage_view(
                self.FUNCTION, "p", offset, type_name, width, alignment, hex(offset)
            )
            tags.append(access)
            texts.append(text)
        include = f'#include "types.h"\n#include "common/draft_fields_{self.FUNCTION}.h"\n'
        return include + "\n".join(tags), texts

    def test_missing_header_is_rebuilt_from_view_tags(self) -> None:
        views = [(0, "s32", 4, 4), (12, "float", 4, 4), (8, "void *", 4, 4), (-4, "s16", 2, 2)]
        source, texts = self.source(*views)
        with tempfile.TemporaryDirectory() as raw:
            row = project(Path(raw))
            made = field_access.restore(row, self.FUNCTION, source)
            assert made is not None
            self.assertEqual(row.work / self.FUNCTION / "include/common" / f"draft_fields_{self.FUNCTION}.h", made)
            body = made.read_text()
        for text in texts:
            self.assertIn(text.split("\n")[1], body)

    def test_existing_header_is_kept_and_unrelated_sources_ignored(self) -> None:
        source, _ = self.source((0, "s32", 4, 4))
        with tempfile.TemporaryDirectory() as raw:
            row = project(Path(raw))
            self.assertIsNone(field_access.restore(row, self.FUNCTION, "int x;\n"))
            kept = row.include[0] / "common" / f"draft_fields_{self.FUNCTION}.h"
            kept.parent.mkdir(parents=True)
            kept.write_text("/* shared */\n")
            self.assertIsNone(field_access.restore(row, self.FUNCTION, source))

    def test_unrecoverable_tag_is_refused_by_name(self) -> None:
        source = (
            f'#include "common/draft_fields_{self.FUNCTION}.h"\n'
            f"int f(void *p) {{ return ((struct Measured_{self.FUNCTION}_{'ab' * 6} *)p)->value; }}\n"
        )
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(Held) as held:
            field_access.restore(project(Path(raw)), self.FUNCTION, source)
        self.assertIn("draft.fields_missing", held.exception.reason)
        self.assertIn("ab" * 6, held.exception.reason)


class ProofGaps(unittest.TestCase):
    def test_empty_provenance_names_each_missing_piece(self) -> None:
        measurement = object.__new__(Measurement)
        measurement.provenance, measurement.strict = {}, {}
        gaps = Measurement.proof_gaps(measurement)
        self.assertIn("source_sha256 missing", gaps)
        self.assertIn("dependencies: record unreadable", gaps)
        self.assertFalse(Measurement.provenance_valid.fget(measurement))


class PooledDeclarations(unittest.TestCase):
    HEADERS: ClassVar[dict[Path, str]] = {
        Path("a.h"): "typedef void (*Hook)(int);\nextern void func_8011F810(int a);\n",
        Path("b.h"): "extern s32 func_8011FEBC;\nextern int other;\n",
    }
    VALUE: ClassVar[dict[str, object]] = {"function_symbols": ["func_8011F810", "func_8011FEBC"], "functions": {}}

    def fake_run(self, host, fn, items, shared=None):
        return [fn(shared, item) for item in items]

    def test_pooled_result_equals_serial_and_second_run_reads_the_cache(self) -> None:
        serial = namespace.FunctionDeclarations(self.VALUE, self.HEADERS)
        with tempfile.TemporaryDirectory() as raw, patch.object(pool, "run", self.fake_run):
            first = namespace.FunctionDeclarations(self.VALUE, self.HEADERS, host=object(), cache_root=Path(raw))
            with patch.object(cdecl, "parse", side_effect=AssertionError("parsed again")):
                second = namespace.FunctionDeclarations(self.VALUE, self.HEADERS, host=object(), cache_root=Path(raw))
        self.assertEqual(serial.prototypes, first.prototypes)
        self.assertEqual(serial.prototypes, second.prototypes)
        self.assertEqual(serial.typedefs, second.typedefs)

    def test_prepared_rewrites_equal_serial_rewrites(self) -> None:
        texts = list(self.HEADERS.values())
        expected = [namespace.FunctionDeclarations(self.VALUE, self.HEADERS).rewrite(t) for t in texts]
        with tempfile.TemporaryDirectory() as raw, patch.object(pool, "run", self.fake_run):
            contracts = namespace.FunctionDeclarations(self.VALUE, self.HEADERS, host=object(), cache_root=Path(raw))
            contracts.prepare(texts, object(), Path(raw))
            self.assertEqual(set(texts), set(contracts.rewritten))
            self.assertEqual(expected, [contracts.rewrite(t) for t in texts])


if __name__ == "__main__":
    unittest.main()
