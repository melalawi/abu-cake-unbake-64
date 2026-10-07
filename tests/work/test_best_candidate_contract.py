"""A search's public Next command compares the real best-file payload as its owning function."""

import argparse
import hashlib
import io
import shlex
from contextlib import contextmanager
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import runner
from unbake.cli import compare as compare_verb
from unbake.cli import search_variants
from unbake.cli.args import Context
from unbake.config import Held
from unbake.layout import split
from unbake.work import compare, search


class BestCandidateContract(ProjectCase):
    versions = ("us",)

    def test_search_next_measures_the_best_payload_with_the_original_unit_and_private_view(self):
        source = self.project.work / "alpha" / "alpha.c"
        source.parent.mkdir(parents=True)
        source.write_text("int alpha(void) { return 9; }\n")
        best = source.with_name("alpha.best.c")
        best.write_text("int alpha(void) { return 1; }\n")
        found = search.Searched("alpha", best, 100.0, True, source.with_suffix(".steps.jsonl"), 1, 0, 0.01)
        context = Context(
            "search-variants",
            argparse.Namespace(file=source, method="permute", seconds=1),
            self.project.root,
            None,
            io.StringIO(),
            self.host,
        )
        with (
            patch.object(Context, "ready", return_value=(self.project, self.host)),
            patch.object(search, "search", return_value=found),
        ):
            result = search_variants.run(context)
        tokens = shlex.split(result.next)
        candidate = best.__class__(tokens[tokens.index("compare") + 1])
        self.assertEqual(candidate, best)
        seen = []

        @contextmanager
        def compile_unit(project, host, file, version, *, unit, **options):
            seen.append((file, unit, version, project.work_include))
            row = compare.row_of(self.project, unit, version)
            obj = self.root / "candidate.o"
            obj.write_bytes(search.target_object(unit, split.words(self.project, row)))
            yield obj

        def link(project, host, obj, version, row, file):
            return split.words(project, row), []

        context = Context(
            "compare",
            argparse.Namespace(file=candidate, require_version=None),
            self.project.root,
            None,
            io.StringIO(),
            self.host,
        )
        with (
            patch.object(Context, "ready", return_value=(self.project, self.host)),
            patch.object(runner, "compile_unit", compile_unit),
            patch.object(runner, "link_function", link),
        ):
            measured = compare_verb.run(context)
        self.assertEqual(measured.data["function"], "alpha")
        self.assertTrue(measured.data["exact"])
        self.assertEqual(measured.data["sha256"], hashlib.sha256(best.read_bytes()).hexdigest())
        self.assertEqual([(file, unit, version) for file, unit, version, _ in seen], [(best, "alpha", "us")])
        self.assertTrue(seen[0][3])
        self.assertEqual(source.read_text(), "int alpha(void) { return 9; }\n")
        self.assertEqual(shlex.split(measured.next)[-1], str(best))

    def test_only_the_search_best_suffix_is_accepted_and_missing_candidates_are_named(self):
        for name in ("alpha.alt.c", "alpha.best.best.c", "alpha.c.txt"):
            file = self.root / name
            file.write_text("int alpha(void) { return 1; }\n")
            with self.assertRaises(Held):
                compare.function_of(file)
        with self.assertRaisesRegex(Held, "missing file"):
            compare.function_of(self.root / "alpha.best.c")
