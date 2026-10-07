"""The real unindexed address headers select one finite ownership snapshot."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import cdecl, pool
from unbake.layout import index
from unbake.typemap import regeneration, storage

FIXTURE = Path(__file__).parents[1] / "fixtures/function_address"
TYPE_SOURCE = "typedef int s32; typedef signed char s8;\n"
ABI = (
    "void func_8011E564(func_8011E564_S1_Shared8011E564 *arg0, s32 (*arg1)(void *), s32 arg2);\n"
    "extern s32 func_8011F810();\n"
)


class ValidationOwnershipWork(TempCase):
    def setUp(self):
        super().setUp()
        self.include = self.root / "include"
        self.segment = self.include / "span_1000"
        self.segment.mkdir(parents=True)
        (self.root / "layout.toml").write_bytes((FIXTURE / "ownership.toml").read_bytes())
        self.objects = self.segment / "code_8011F7F0.h"
        self.argument = self.segment / "code_8011E3F0.h"
        self.outputs = {
            self.objects: (FIXTURE / "objects.h").read_bytes(),
            self.argument: (FIXTURE / "argument.h").read_bytes(),
        }
        for path, data in self.outputs.items():
            path.write_bytes(data)
        self.types = self.include / "types.h"
        self.types.write_text(TYPE_SOURCE)
        self.authored = {self.types: TYPE_SOURCE}
        self.project = SimpleNamespace(root=self.root, include=(self.include,), build=self.root / "build")
        self.assertFalse(index.path(self.project).exists())

    def inputs(self, *, authored=True):
        return regeneration.validation_inputs(
            self.project, self.outputs, ABI, authored=self.authored if authored else None
        )

    def test_real_missing_index_recovers_once_and_classifies_each_header_once(self):
        read_text, read_bytes, snapshot = Path.read_text, Path.read_bytes, storage.generated_view
        texts, bytes_, classified = Counter(), Counter(), []

        def text(path, *args, **kwargs):
            texts[path] += 1
            return read_text(path, *args, **kwargs)

        def data(path, *args, **kwargs):
            bytes_[path] += 1
            return read_bytes(path, *args, **kwargs)

        def view(project):
            generated = snapshot(project)

            def classify(path):
                classified.append(path)
                return generated(path)

            return classify

        with (
            patch.object(storage, "generated_view", side_effect=view) as views,
            patch.object(index, "_unindexed_headers", wraps=index._unindexed_headers) as scans,
            patch.object(Path, "glob", autospec=True, side_effect=Path.glob) as globs,
            patch.object(Path, "read_text", text),
            patch.object(Path, "read_bytes", data),
            patch.object(cdecl, "declarations", wraps=cdecl.declarations) as declarations,
            patch.object(pool, "run", side_effect=AssertionError("worker start")) as workers,
        ):
            contents, closures, abi = self.inputs()
        self.assertEqual(views.call_count, 1)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(globs.call_count, 3)  # common and each of the two real groups
        self.assertEqual(texts, {self.root / "layout.toml": 1, self.objects: 1, self.argument: 1, self.types: 1})
        self.assertEqual(bytes_, {})
        self.assertEqual(Counter(classified), {path: 1 for path in contents})
        self.assertEqual(declarations.call_count, 3)
        self.assertEqual(workers.call_count, 0)
        self.assertEqual(contents, {**self.outputs, self.types: TYPE_SOURCE.encode()})
        self.assertEqual(closures[self.objects], {self.objects, self.types})
        self.assertEqual(closures[self.argument], {self.argument, self.types})
        self.assertEqual(abi[0][1], {self.argument, self.types})
        self.assertEqual(abi[1][1], {self.objects, self.types})

    def test_authored_discovery_uses_the_same_snapshot_as_provider_selection(self):
        classified = []
        snapshot = storage.generated_view

        def view(project):
            generated = snapshot(project)

            def classify(path):
                classified.append(path)
                return generated(path)

            return classify

        with (
            patch.object(storage, "generated_view", side_effect=view) as views,
            patch.object(index, "_unindexed_headers", wraps=index._unindexed_headers) as scans,
            patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as reads,
        ):
            contents, _closures, abi = self.inputs(authored=False)
        self.assertEqual(views.call_count, 1)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(Counter(classified), {path: 1 for path in contents})
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(reads.call_args.args, (self.types,))
        self.assertEqual(abi[1][1], {self.objects, self.types})

    def memo_boundary(self):
        """Control only the existing graph memo; its implementation has another owner."""
        retained, identities, computes = {}, [], []

        def memo(kind, identity, compute, **kwargs):
            if kind != "typemap.validation-graph":
                return compute()
            identities.append(identity)
            if identity not in retained:
                computes.append(identity)
                retained[identity] = compute()
            return retained[identity]

        return memo, identities, computes

    def test_equal_snapshot_hits_existing_graph_memo_without_reparsing(self):
        memo, identities, computes = self.memo_boundary()
        with (
            patch.object(regeneration, "memo", side_effect=memo),
            patch.object(storage, "generated_view", wraps=storage.generated_view) as views,
            patch.object(index, "_unindexed_headers", wraps=index._unindexed_headers) as scans,
            patch.object(cdecl, "declarations", wraps=cdecl.declarations) as parses,
        ):
            first = self.inputs()
            second = self.inputs()
        self.assertEqual(first, second)
        self.assertEqual(views.call_count, 2)
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(parses.call_count, 3)
        self.assertEqual(len(identities), 2)
        self.assertEqual(len(computes), 1)

    def test_ownership_change_invalidates_provider_graph_even_when_bytes_stay_equal(self):
        # Both receipts are real declarations; generation precedence determines
        # which one owns the ABI's ordinary identifier.
        duplicate = "extern s32 func_8011F810;\n"
        self.types.write_text(TYPE_SOURCE + duplicate)
        self.authored[self.types] += duplicate
        memo, identities, computes = self.memo_boundary()
        generation = {self.objects, self.argument}

        def view(project):
            selected = frozenset(generation)
            return lambda path: path in selected

        with (
            patch.object(regeneration, "memo", side_effect=memo),
            patch.object(storage, "generated_view", side_effect=view),
        ):
            first = self.inputs()
            generation.clear()
            generation.add(self.types)
            second = self.inputs()
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2][1][1], {self.objects, self.types})
        self.assertEqual(second[2][1][1], {self.types})
        self.assertEqual(len(set(identities)), 2)
        self.assertEqual(len(computes), 2)

    def test_guarded_include_cycle_keeps_finite_complete_closures(self):
        self.outputs[self.objects] = self.outputs[self.objects].replace(
            b'#include "../types.h"', b'#include "../types.h"\n#include "code_8011E3F0.h"'
        )
        self.outputs[self.argument] = self.outputs[self.argument].replace(
            b'#include "../types.h"', b'#include "../types.h"\n#include "code_8011F7F0.h"'
        )
        with (
            patch.object(storage, "generated_view", wraps=storage.generated_view) as views,
            patch.object(index, "_unindexed_headers", wraps=index._unindexed_headers) as scans,
        ):
            contents, closures, abi = self.inputs()
        self.assertEqual(views.call_count, 1)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(closures[self.objects], set(contents))
        self.assertEqual(closures[self.argument], set(contents))
        self.assertTrue(all(selected == set(contents) for _text, selected in abi))
