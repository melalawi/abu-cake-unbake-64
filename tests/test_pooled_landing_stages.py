"""Per-version landing work is one pool job per version, and one command reads its dependencies once."""

import pickle
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from unbake import cache, pool
from unbake.cdecl import LayoutParser
from unbake.config import Held
from unbake.fold import declarations, source_views
from unbake.inputs import DependencySet
from unbake.process import named
from unbake.work import compare

VERSIONS = ("us", "jp", "eu", "au")
CONDITIONAL = "#if VERSION == 1\nint a;\n#else\nint b;\n#endif\n"


class Recorder:
    """A pool.run stand-in: records each fan-out and runs it serially through a pickle round trip."""

    def __init__(self) -> None:
        self.calls: list[tuple[Callable[..., Any], int]] = []

    def __call__(self, host: Any, fn: Callable[..., Any], items: Sequence[Any], shared: Any = None) -> list[Any]:
        self.calls.append((fn, len(items)))
        done = [fn(shared, pickle.loads(pickle.dumps(item))) for item in items]
        return list(pickle.loads(pickle.dumps(done)))


class ParsePerVersion(unittest.TestCase):
    def parse(self, views: dict[str, str]) -> tuple[list[LayoutParser], Recorder, MagicMock]:
        recorder = Recorder()
        context = MagicMock()
        context.parse.side_effect = lambda view: (LayoutParser(view), [])
        project = MagicMock()
        with (
            patch.object(pool, "run", recorder),
            patch.object(source_views, "active_source", side_effect=lambda p, h, t, v, u, c: views[v]),
        ):
            return source_views.parsers(project, MagicMock(), CONDITIONAL, VERSIONS, "unit", context), recorder, context

    def test_one_job_per_version_and_identical_views_share_the_first_parser(self) -> None:
        views = {"us": "int a;\n", "jp": "int b;\n", "eu": "int a;\n", "au": "int b;\n"}
        parsers, recorder, _ = self.parse(views)
        self.assertEqual([(source_views._version_parser, 4)], recorder.calls)
        self.assertEqual(4, len(parsers))
        self.assertIs(parsers[0], parsers[2])
        self.assertIs(parsers[1], parsers[3])
        self.assertIsNot(parsers[0], parsers[1])
        self.assertEqual(["int a;\n", "int b;\n", "int a;\n", "int b;\n"], [p.source for p in parsers])

    def test_source_without_conditionals_is_parsed_once_without_the_pool(self) -> None:
        recorder = Recorder()
        context = MagicMock()
        context.parse.side_effect = lambda view: (LayoutParser(view), [])
        with patch.object(pool, "run", recorder):
            result = source_views.parsers(MagicMock(), MagicMock(), "int a;\n", VERSIONS, "unit", context)
        self.assertEqual([], recorder.calls)
        self.assertEqual(1, len(result))
        self.assertEqual(1, context.parse.call_count)


class Transport(unittest.TestCase):
    def test_a_parser_survives_the_worker_boundary(self) -> None:
        source = "typedef struct A { int x; short y; } A;\nA value;\n"
        parser = LayoutParser(source)
        parser.parse()
        copy = pickle.loads(pickle.dumps(parser))
        self.assertEqual([t[0] for t in parser.tokens], [t[0] for t in copy.tokens])
        self.assertEqual([(t.start(), t.end()) for t in parser.tokens], [(t.start(), t.end()) for t in copy.tokens])
        self.assertEqual([a.name for a in parser.aggregates], [a.name for a in copy.aggregates])
        self.assertEqual([(d.start, d.end) for d in parser.declarations], [(d.start, d.end) for d in copy.declarations])


class NamesPerVersion(unittest.TestCase):
    def names(self, plans: dict[str, declarations.VersionNames]) -> tuple[str, set[str], Recorder]:
        recorder = Recorder()
        headers = MagicMock()
        headers.texts = {}
        parsers = [LayoutParser(f"int {version};\n") for version in VERSIONS]
        seen: list[str] = []

        def plan(state: Any, item: tuple[str, LayoutParser]) -> declarations.VersionNames:
            seen.append(item[0])
            return plans[item[0]]

        plan.__name__ = "_version_names"
        with patch.object(pool, "run", recorder), patch.object(declarations, "_version_names", plan):
            text, tags = declarations._layout_names(
                MagicMock(), MagicMock(), "fn", "abcdefghij", parsers, VERSIONS, headers
            )
        self.assertEqual(list(VERSIONS), seen)
        return text, tags, recorder

    @staticmethod
    def plan(planned: dict[tuple[int, int], str], tags: frozenset[str] = frozenset()) -> declarations.VersionNames:
        return declarations.VersionNames(planned, {}, frozenset(), frozenset(), tags)

    def test_each_version_is_one_job_and_the_merge_equals_the_serial_loop(self) -> None:
        plans = {
            "us": self.plan({(0, 1): "X"}, frozenset({"A"})),
            "jp": self.plan({(0, 1): "X", (2, 3): "Y"}),
            "eu": self.plan({}, frozenset({"B"})),
            "au": self.plan({(4, 5): "Z"}),
        }
        text, tags, recorder = self.names(plans)
        self.assertEqual([("_version_names", 4)], [(fn.__name__, n) for fn, n in recorder.calls])
        self.assertEqual("XbYdZfghij", text)
        self.assertEqual({"A", "B"}, tags)

    def test_a_version_dependent_rename_of_one_token_is_still_refused(self) -> None:
        plans = {
            "us": self.plan({(0, 1): "X"}),
            "jp": self.plan({(0, 1): "Y"}),
            "eu": self.plan({}),
            "au": self.plan({}),
        }
        with self.assertRaises(Held) as raised:
            self.names(plans)
        self.assertIn("version-dependent layout rename", str(raised.exception))

    def test_a_worker_runs_nested_fan_out_inline(self) -> None:
        calls: list[int] = []

        def record(value: int) -> int:
            calls.append(value)
            return value

        with (
            patch.object(pool, "_token", "token"),
            patch.object(pool.Pool, "from_host", side_effect=AssertionError("no nested pool")),
        ):
            result = pool.run(MagicMock(), record, [1, 2, 3])
        self.assertEqual([1, 2, 3], result)
        self.assertEqual([1, 2, 3], calls)


class DependenciesOncePerState(unittest.TestCase):
    def setUp(self) -> None:
        cache.configure(memory_bytes=16 * 1024 * 1024)
        cache.forget()
        self.addCleanup(cache.forget)

    def read(self, state: list[str]) -> tuple[list[Any], MagicMock]:
        counted = MagicMock(side_effect=lambda *a: object())
        counted.side_effect = lambda *a: DependencySet((), {"n": counted.call_count}, {})
        with (
            patch.object(compare, "dependency_state", lambda *a: state[0]),
            patch.object(compare, "_operation_dependencies", counted),
        ):
            reads = [compare.operation_dependencies(MagicMock(), MagicMock(), Path("f.c")) for _ in range(5)]
            state[0] = "after the tool wrote a file"
            reads.append(compare.operation_dependencies(MagicMock(), MagicMock(), Path("f.c")))
        return reads, counted

    def test_five_reads_of_one_tree_state_compute_once_and_a_write_recomputes(self) -> None:
        reads, counted = self.read(["tree state"])
        self.assertEqual(2, counted.call_count)
        self.assertTrue(all(read == reads[0] for read in reads[:5]))
        self.assertNotEqual(reads[0], reads[5])

    def test_a_retained_answer_is_a_copy(self) -> None:
        reads, _ = self.read(["tree state"])
        self.assertIsNot(reads[0], reads[1])


class IncluderProofJobs(unittest.TestCase):
    """The header includer proof is one pool job per includer, version and branch, not a serial loop."""

    def test_jobs_are_includer_version_branch_and_a_failure_names_its_job(self) -> None:
        from unbake.layout import structs_fold

        self.assertTrue(getattr(structs_fold._prove_job, "_pool_worker", False))
        project = MagicMock()
        project.root = Path("/p")
        project.compiler_for.return_value.id = "cc"
        project.compiler_for.return_value.cc = "cc1"
        project.cppflags = ()
        overlay = Path("/o")
        calls: list[list[str]] = []
        commands = MagicMock(preprocess=("cpp",), compile=("cc",), assemble=None)
        with (
            patch("unbake.compilers.drivers.from_flags", return_value=commands),
            patch("unbake.compilers.drivers.assembly_flags", return_value=()),
            patch("unbake.compilers.drivers.Tools"),
            patch("unbake.process.run_tool", side_effect=lambda argv, *a, **k: calls.append(argv) or ""),
            patch("unbake.atomic.text"),
        ):
            ok = structs_fold._prove_job((project, MagicMock(), overlay), (0, Path("/p/src/a.c"), "us", [], False))
            self.assertIsNone(ok)
            self.assertEqual([["cpp"], ["cc"]], calls)
        with (
            patch("unbake.compilers.drivers.from_flags", return_value=commands),
            patch("unbake.compilers.drivers.assembly_flags", return_value=()),
            patch("unbake.compilers.drivers.Tools"),
            patch("unbake.process.run_tool", side_effect=Held(named("t", "boom", owner="t", stage="t"))),
            patch("unbake.atomic.text"),
        ):
            reason = structs_fold._prove_job((project, MagicMock(), overlay), (1, Path("/p/src/a.c"), "jp", [], True))
        self.assertIn("VERSION jp NON_MATCHING=1", reason or "")
