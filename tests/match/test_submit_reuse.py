"""Reuse immutable header analyses while preserving ordered fold decisions."""

import tempfile
import unittest
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.decomp import header_declarations
from unbake.layout.header_context import Headers
from unbake.layout.structs import layouts
from unbake.layout.structs_parser import Parser
from unbake.match import batch_fold, declarations, forked, rewrite_view, source_views, type_rewrite
from unbake.project import cache
from unbake.project.config import Held
from unbake.project.headers import include_headers
from unbake.typemap import declarations as typed_declarations
from unbake.typemap import header_names


class HeaderReuseTests(unittest.TestCase):
    def test_content_reuse_observes_edits_and_keeps_caller_mutations_private(self):
        for operation, parser, mutate in (
            (header_declarations.declarations, header_declarations.Parser, lambda result: result.typedefs.clear()),
            (header_names.alias_types, header_names._Declarations, lambda result: result.clear()),
        ):
            with self.subTest(operation=operation.__name__), patch.object(cache, "_remembered", {}):
                source = "typedef int Word;"
                implementation = parser.parse
                with patch.object(parser, "parse", autospec=True, side_effect=implementation) as parse:
                    first = operation(source)
                    mutate(first)
                    forked.release(shared=True)
                    second = operation(source)
                    self.assertTrue(second.typedefs if hasattr(second, "typedefs") else second)
                    self.assertEqual(parse.call_count, 1)
                    operation(source + "\n")
                    self.assertEqual(parse.call_count, 2)

    def test_generated_directories_are_pruned_before_resolution_with_overlay_precedence(self):
        for overlay in (False, True):
            with self.subTest(overlay=overlay), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fallback = root / "include"
                first = root / "overlay" if overlay else fallback
                for path, text in (
                    (fallback / "types.h", "base"),
                    (first / "types.h", "selected"),
                    (first / "shared/types/unused.h", "excluded"),
                    (first / "shared/decls/unused.h", "excluded"),
                    (first / "nested/own.h", "own"),
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                project = SimpleNamespace(
                    include=(first, fallback) if overlay else (first,), overlay_roots=(first,) if overlay else ()
                )

                def excluded(path):
                    return path.name in {"types", "decls"}

                resolve = Path.resolve
                resolved = []

                def resolving(path, *args, resolved=resolved, resolve=resolve, **kwargs):
                    resolved.append(path)
                    return resolve(path, *args, **kwargs)

                with patch.object(Path, "resolve", new=resolving):
                    result = include_headers(project, exclude=excluded)
                self.assertEqual([relative for _, relative in result], ["nested/own.h", "types.h"])
                self.assertEqual(dict((relative, path.read_text()) for path, relative in result)["types.h"], "selected")
                self.assertFalse(any(path.name == "unused.h" for path in resolved))

    def test_shared_release_keeps_only_bounded_reusable_contexts(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                remembered = {
                    kind: OrderedDict((str(i), i) for i in range(10))
                    for kind in (
                        "headers.aliases",
                        "headers.declarations",
                        "headers.context",
                        "rewrite.plans",
                        "parsed.split.functions",
                        "source.temporary",
                    )
                }
                parsed = {("split.layout", (Path(str(i)),), None): ((), i) for i in range(20)}
                parsed[("candidate", (), None)] = ((), None)
                with (
                    patch.object(cache, "_remembered", remembered),
                    patch.object(cache, "_parsed", parsed),
                    patch.object(type_rewrite, "_context") as context,
                ):
                    forked.release(shared=shared)
                if shared:
                    self.assertEqual(len(parsed), 16)
                    self.assertEqual(len(remembered["headers.aliases"]), 10)
                    self.assertEqual(list(remembered["headers.context"]), ["9"])
                    self.assertEqual(len(remembered["parsed.split.functions"]), 8)
                    self.assertNotIn("source.temporary", remembered)
                    context.cache_clear.assert_not_called()
                else:
                    self.assertEqual(parsed, {})
                    self.assertEqual(set(remembered), {"headers.aliases", "headers.declarations"})
                    context.cache_clear.assert_called_once_with()

    def test_serial_fold_does_each_candidate_once_and_rolls_back_a_hold(self):
        for refused in (None, "alpha", "beta"):
            with self.subTest(refused=refused), patch.object(cache, "_remembered", {}):
                root = Path("project")
                path = root / "include/types.h"
                headers = Headers({path: "typedef int Word;"}, root=root)
                candidates = [SimpleNamespace(function=name) for name in ("alpha", "beta")]
                calls = []
                receipts = []

                def fold(staged, policy, context, candidate, changes, calls=calls, path=path, refused=refused):
                    calls.append((candidate.function, context.texts[path]))
                    context.apply([SimpleNamespace(path=path, after=f"typedef int {candidate.function};")])
                    if candidate.function == refused:
                        raise Held("fold", "same reason")
                    return declarations.Folded(candidate.function, candidate.function, [], {})

                with (
                    patch.object(source_views, "shared_includes", return_value=nullcontext()),
                    patch.object(batch_fold, "_fold_one", side_effect=fold),
                    patch.object(batch_fold, "_speculate", side_effect=AssertionError("speculation")),
                    patch.object(batch_fold, "_warm_contexts", side_effect=AssertionError("warmup")),
                    patch.object(forked, "release") as release,
                ):
                    stream = batch_fold.fold(None, SimpleNamespace(cores=1), headers, candidates, receipts)
                    self.assertEqual(calls, [])
                    result = list(stream)
                self.assertEqual([name for name, _ in calls], ["alpha", "beta"])
                self.assertEqual(
                    [c.function for c, _ in result], [c.function for c in candidates if c.function != refused]
                )
                self.assertEqual(calls[1][1], "typedef int Word;" if refused == "alpha" else "typedef int alpha;")
                self.assertEqual(
                    headers.texts[path], "typedef int alpha;" if refused == "beta" else "typedef int beta;"
                )
                self.assertEqual(
                    receipts, [] if refused is None else [f"HELD(submit): {refused}: submit.fold: same reason"]
                )
                self.assertEqual(release.call_count, 2)


class MaterializedIncludesTests(unittest.TestCase):
    def test_shared_snapshot_writes_changes_only_and_removes_rolled_back_additions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            main, added = original / "types.h", original / "new.h"
            project = SimpleNamespace(root=root, include=(original,))
            headers = Headers({main: "typedef int Word;"}, root=root)
            write = Path.write_text
            writes = []

            def writing(path, text, *args, **kwargs):
                writes.append((path, text))
                return write(path, text, *args, **kwargs)

            with source_views.shared_includes(project, headers), patch.object(Path, "write_text", new=writing):
                previous = None
                for edits, expected_writes in (
                    ({}, 1),
                    ({}, 0),
                    ({main: "typedef float Word;"}, 1),
                    ({added: "typedef int New;"}, 1),
                    ({added: None}, 0),
                    ({main: "typedef int Word;"}, 1),
                ):
                    before = len(writes)
                    for path, text in edits.items():
                        if text is None:
                            del headers.texts[path]
                        else:
                            headers.texts[path] = text
                    roots = source_views.header_includes(project, headers, root / "unused")
                    self.assertEqual(len(writes) - before, expected_writes)
                    self.assertEqual((roots[0] / "types.h").read_text(), headers.texts[main])
                    self.assertEqual((roots[0] / "new.h").exists(), added in headers.texts)
                    if previous is not None:
                        self.assertEqual(roots, previous)
                    previous = roots
            self.assertNotIn("_shared_includes", headers.__dict__)
            self.assertFalse(previous[0].exists())
            self.assertFalse(original.exists())

    def test_supplied_header_contents_avoid_discovery_and_keep_preprocessor_inputs(self):
        from tests.match.support import MatchFixture
        from tests.match.test_rewrite_view import dump

        fixture = MatchFixture()
        fixture.setUp()
        try:
            project, policy = fixture.project, fixture.policy
            header = project.include[0] / "types.h"
            contents = {header: "typedef int Word;"}
            with (
                patch.object(typed_declarations, "include_headers", side_effect=AssertionError("discovery")),
                patch.object(typed_declarations, "_preprocess", return_value="typed") as cpp,
            ):
                self.assertEqual(typed_declarations.headers(project, policy, "us", contents=contents), "typed")
                self.assertIn(str(header), cpp.call_args.args[-1])
            source = "int alpha(void) {return 1;}"
            path = project.src / "alpha.c"
            output = dump([(rewrite_view._BOUNDARY, "<stdin>", 1, 1), (";", "<stdin>", 1, 2), ("int", str(path), 1, 1)])
            with (
                patch("unbake.project.headers.include_headers", side_effect=AssertionError("discovery")),
                patch.object(typed_declarations, "_generated_context", return_value=[]),
            ):
                commands = []
                result = rewrite_view.prepare(
                    project,
                    policy,
                    source,
                    "us",
                    path,
                    contents=contents,
                    preprocess=lambda p, cmd, unit: commands.append((cmd, unit)) or output,
                )
            self.assertEqual(result.origins, (0,))
            self.assertIn(str(header), commands[0][1])
            self.assertIn(source, commands[0][1])
            self.assertIn("-fdebug-cpp", commands[0][0])
        finally:
            fixture.tearDown()
            fixture.doCleanups()


class RewritePlanReuseTests(unittest.TestCase):
    def test_plans_reuse_equal_effective_views_and_invalidate_semantic_or_provenance_changes(self):
        source = "typedef struct Old {int member;} Old;"
        original = layouts(source)[0]
        evidence = replace(original, name="Canon")
        view = rewrite_view.View("Old\n", (rewrite_view.Location("source.c", 1, 1),), (0,))
        changes = (
            {"context": "typedef float Other;"},
            {"preprocess": lambda: replace(view, text="Other\n")},
            {"preprocess": lambda: replace(view, origins=(1,))},
            {"preprocess": lambda: replace(view, locations=(rewrite_view.Location("other.c", 1, 1),))},
            {"resolution": {"Old": ("Other", evidence)}},
            {"tag_only": {"Canon"}},
            {"typedef_renames": {"Callback": "OtherCallback"}},
            {"source_path": Path("other.c")},
            {"source_line_offset": 1},
            {"source_text": source + "\n"},
            {"cache_root": Path("other-cache")},
        )
        for changed in changes:
            with self.subTest(changed=tuple(changed)), patch.object(cache, "_remembered", {}):
                parser = Parser(source)
                parser.parse()
                args = dict(
                    context="typedef int Other;",
                    resolution={"Old": ("Canon", evidence)},
                    preprocess=lambda: view,
                    source_path=Path("source.c"),
                    source_text=source,
                )
                with patch.object(type_rewrite, "_plan", return_value={(0, 3): "Canon"}) as plan:
                    first = type_rewrite.edits(parser, **args)
                    first.clear()
                    self.assertEqual(type_rewrite.edits(parser, **args), {(0, 3): "Canon"})
                    self.assertEqual(plan.call_count, 1)
                    type_rewrite.edits(parser, **{**args, **changed})
                    self.assertEqual(plan.call_count, 2)


class RetainedDependencyReuseTests(unittest.TestCase):
    def test_dependency_snapshot_checks_shared_paths_once_and_never_hides_changes(self):
        import hashlib
        import json
        import os

        from unbake.match import incremental

        cases = (
            ("same", []),
            ("digest_changed", ["alpha", "beta"]),
            ("newer", ["alpha", "beta"]),
            ("missing", ["alpha", "beta"]),
            ("directory", ["alpha", "beta"]),
            ("source_changed", ["alpha"]),
            ("compiler_changed", ["alpha", "beta"]),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original_root, staged_root = root / "original", root / "staged"
                generation = root / "generation"
                source_paths = (original_root / "src", staged_root / "src")
                for path in (*source_paths, generation / "obj/src", staged_root / "include"):
                    path.mkdir(parents=True)
                header = staged_root / "include/common.h"
                header.write_text("typedef int Word;")
                os.utime(header, ns=(1, 1))
                if kind == "missing":
                    header.unlink()
                elif kind == "directory":
                    header.unlink()
                    header.mkdir()
                elif kind == "newer":
                    os.utime(header, ns=(100, 100))
                for name in ("alpha", "beta"):
                    for path in source_paths:
                        (path / (name + ".c")).write_text("int " + name + "(void) {return 0;}")
                    if kind == "source_changed" and name == "alpha":
                        (source_paths[1] / "alpha.c").write_text("int alpha(void) {return 1;}")
                    obj = generation / "obj/src" / (name + ".o")
                    obj.touch()
                    obj.with_suffix(".built").touch()
                    os.utime(obj.with_suffix(".built"), ns=(10, 10))
                    obj.with_suffix(".d").write_text("unit.o: include/common.h include/common.h\n")
                    if kind in ("same", "digest_changed"):
                        digest = hashlib.sha256(b"typedef int Word;").hexdigest()
                        if kind == "digest_changed":
                            digest = "0" * 64
                        obj.with_suffix(".inputs.json").write_text(json.dumps({"include/common.h": digest}))
                split_file = staged_root / "split.yaml"
                split_file.write_text("retained")
                original = SimpleNamespace(
                    root=original_root, src=source_paths[0], compiler_for=lambda path: SimpleNamespace(id="cc")
                )
                staged = SimpleNamespace(
                    root=staged_root,
                    src=source_paths[1],
                    version=lambda version, split_file=split_file: SimpleNamespace(split=split_file),
                    compiler_for=lambda path, kind=kind: SimpleNamespace(
                        id="other" if kind == "compiler_changed" else "cc"
                    ),
                )
                stat = Path.stat
                visits = []

                def observed(path, *args, header=header, visits=visits, stat=stat, **kwargs):
                    if path == header:
                        visits.append(path)
                    return stat(path, *args, **kwargs)

                with (
                    patch.object(incremental.extract, "unit_ranges", return_value={"alpha": {}, "beta": {}}),
                    patch.object(Path, "stat", new=observed),
                ):
                    changed = incremental.changed_sources(original, staged, generation, "us")
                self.assertEqual([path.stem for path in changed], expected)
                self.assertEqual(len(visits), 1)
