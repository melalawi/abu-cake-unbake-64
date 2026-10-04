"""Historical declaration reuse retains source isolation and type precedence."""

import copy
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.project.config import Held
from unbake.typemap import declarations, solver, storage


class FeedbackPreprocessingTests(unittest.TestCase):
    def fixture(self, root):
        include = root / "include"
        include.mkdir()
        scratch = root / "scratch"
        scratch.mkdir()
        batch = declarations._PublishedHeaders.__new__(declarations._PublishedHeaders)
        batch.project = SimpleNamespace(root=root, include=(include,))
        batch.policy = SimpleNamespace(cores=12)
        batch.scratch = scratch
        batch.replay = True
        batch.guarded = {}
        batch.batch_headers = {}
        batch.batch_guards = {}
        batch.batch_effects = {}
        batch.batch_directives = {}
        batch.prepared = {v: ("", ["mock-cpp", "-P", "-x", v, "-"]) for v in ("us", "eu")}
        batch.macros = {v: {"VALUE": f"#define VALUE {value}\n"} for v, value in (("us", "int"), ("eu", "float"))}
        return batch

    def cpp(self, batch, calls, mode="valid"):
        def run(project, command, text):
            version = command[command.index("-x") + 1]
            values = {}
            for flag in command:
                if flag.startswith("-D"):
                    name, _, value = flag[2:].partition("=")
                    values[name] = value or "1"
            for name, line in batch.macros[version].items():
                values[name] = line.split(maxsplit=2)[2].strip()
            calls.append(text)

            def expand(text):
                output = []
                active = True
                stack = []
                for line in text.splitlines():
                    directive = re.match(r"\s*#\s*(\w+)\s*(.*)", line)
                    if directive:
                        kind, argument = directive.groups()
                        if kind in {"if", "ifdef", "ifndef"}:
                            condition = argument != "0" if kind == "if" else argument in values
                            if kind == "ifndef":
                                condition = not condition
                            stack.append((active, condition))
                            active = active and condition
                        elif kind == "else":
                            parent, condition = stack[-1]
                            active = parent and not condition
                        elif kind == "endif":
                            active = stack.pop()[0]
                        elif active and kind == "define":
                            name, _, value = argument.partition(" ")
                            values[name] = value
                        elif active and kind == "undef":
                            values.pop(argument, None)
                        elif active and kind == "error":
                            raise Held("solve", "types.declaration: rejected source")
                        elif active and kind == "include":
                            argument = re.sub(r"\b\w+\b", lambda m: values.get(m[0], m[0]), argument)
                            relative = argument.strip('"<>')
                            candidate = Path(relative)
                            if not candidate.is_file():
                                candidate = project.include[0] / relative
                            output.append(expand(candidate.read_text()))
                        continue
                    if active:
                        output.append(re.sub(r"\b\w+\b", lambda m: values.get(m[0], m[0]), line))
                return "\n".join(output) + "\n"

            result = expand(text)
            if "__unbake_feedback_unit_" in text:
                if mode == "error":
                    raise Held("solve", "types.declaration: grouped source rejected")
                if mode == "missing":
                    result = re.sub(r"extern int __unbake_feedback_unit_\w+_0;", "", result)
                if mode == "duplicate":
                    marker = re.search(r"extern int __unbake_feedback_unit_\w+_0;", result)[0]
                    result = marker + "\n" + result
            return result

        return run

    def test_macro_state_versions_order_and_failed_batches_match_independent_sources(self):
        cases = {
            "define": "#define VALUE double\n",
            "undef": "#undef VALUE\n#define VALUE unsigned\n",
            "conditional": "#if 0\n#define VALUE double\n#endif\n",
            "closure": '#include "mutate.h"\n',
            "guard": '#undef GUARD\n#include "mutate.h"\n',
            "consumer": "#ifdef CONSUMER\n#define VALUE double\n#endif\n",
            "local_hold": "#error rejected\n",
        }
        for case, prefix in cases.items():
            for reverse in (False, True):
                for mode in ("valid", "error", "missing", "duplicate"):
                    with self.subTest(case=case, reverse=reverse, mode=mode), tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        batch = self.fixture(root)
                        (root / "include/mutate.h").write_text(
                            "#ifndef GUARD\n#define GUARD\n#define VALUE double\n#endif\n"
                        )
                        if case == "guard":
                            for macros in batch.macros.values():
                                macros["GUARD"] = "#define GUARD 1\n"
                        a, b = root / "alpha.c", root / "beta.c"
                        a.write_text(prefix + "VALUE alpha(void) { return 0; }\n")
                        b.write_text("VALUE beta(void) { return 0; }\n")
                        sources = [b, a] if reverse else [a, b]
                        calls = []
                        with patch.object(declarations, "_preprocess", side_effect=self.cpp(batch, calls, mode)):
                            for version in ("us", "eu"):
                                expected = []
                                for source in sources:
                                    try:
                                        expected.append(batch.source(version, source))
                                    except Held as error:
                                        expected.append(error)
                                actual = batch.batch(version, sources)
                                for _source, old, new in zip(sources, expected, actual, strict=True):
                                    if isinstance(old, Held):
                                        self.assertIsInstance(new, Held)
                                        self.assertEqual(new.reason, old.reason)
                                    else:
                                        old_seed = declarations.extract("".join(old), {})
                                        new_seed = declarations.extract("".join(new), {})
                                        self.assertEqual(storage.encoded(old_seed), storage.encoded(new_seed))
                        self.assertEqual(list(batch.scratch.iterdir()), [])

    def test_stateful_unresolved_and_builtin_effects_stay_independent(self):
        cases = [
            "#pragma once\n",
            "#include_next <missing>\n",
            "#import <missing>\n",
            "#assert cpu(mips)\n",
            "#include HEADER\n",
            '#include "absent.h"\n',
            "#undef __LINE__\n",
            "__COUNTER__;\n",
            '_Pragma("pack(1)");\n',
            "__INCLUDE_LEVEL__;\n",
        ]
        for text in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                batch = self.fixture(root)
                source = root / "alpha.c"
                source.write_text(text + "int alpha(void) { return 0; }\n")
                self.assertIsNone(batch._batch_input("us", source))
                with patch.object(batch, "source", return_value=("", "int alpha(void) {}")) as individual:
                    self.assertEqual(batch.batch("us", [source]), [("", "int alpha(void) {}")])
                    individual.assert_called_once_with("us", source)

    def test_consumer_macro_and_transitive_header_macros_are_restored(self):
        from unbake.typemap.split import consumer_macro

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = self.fixture(root)
            consumer = root / "include/shared/consumers"
            consumer.mkdir(parents=True)
            (consumer / "alpha.h").write_text("")
            macro = consumer_macro("alpha")
            (root / "include/outer.h").write_text('#include "inner.h"\n')
            (root / "include/inner.h").write_text(f"#ifdef {macro}\n#define VALUE double\n#endif\n")
            sources = [root / "alpha.c", root / "beta.c"]
            for path in sources:
                path.write_text(f'#include "outer.h"\nVALUE {path.stem}(void) {{ return 0; }}\n')
            with patch.object(declarations, "_preprocess", side_effect=self.cpp(batch, [])):
                actual = batch.batch("us", sources)
            self.assertIn("double alpha", actual[0][1])
            self.assertIn("int beta", actual[1][1])

    def test_cached_header_effects_observe_later_guard_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = self.fixture(root)
            batch.macros["us"]["GUARD"] = "#define GUARD 1\n"
            (root / "include/mutate.h").write_text("#ifndef GUARD\n#define GUARD\n#define VALUE double\n#endif\n")
            ordinary, altered = root / "alpha.c", root / "beta.c"
            ordinary.write_text('#include "mutate.h"\nVALUE alpha(void) {}\n')
            altered.write_text('#undef GUARD\n#include "mutate.h"\nVALUE beta(void) {}\n')
            calls = []
            with patch.object(declarations, "_preprocess", side_effect=self.cpp(batch, calls)):
                for version in ("us", "eu", "us", "eu"):
                    for sources in ([ordinary, altered], [altered, ordinary], [ordinary, ordinary]):
                        actual = batch.batch(version, sources)
                        for path, (_, text) in zip(sources, actual, strict=True):
                            expected = "double" if path == altered or version == "eu" else "int"
                            self.assertIn(f"{expected} {path.stem}", text)
            self.assertTrue(all(len(entries) <= 4 for entries in batch.batch_effects.values()))

    def test_concurrent_header_analysis_publishes_complete_cache_entries(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = self.fixture(root)
            (root / "include/mutate.h").write_text("#ifndef GUARD\n#define GUARD\n#define VALUE double\n#endif\n")
            source = root / "alpha.c"
            source.write_text('#include "mutate.h"\nVALUE alpha(void) {}\n')
            guard = declarations._outer_guard
            barrier = Barrier(2)

            def simultaneous(text):
                barrier.wait(timeout=2)
                return guard(text)

            with patch.object(declarations, "_outer_guard", side_effect=simultaneous), ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda version: batch._batch_input(version, source), ["us", "eu"]))
            self.assertTrue(all(row is not None and "VALUE" in row[1] for row in results))

    def test_receipt_order_and_bounded_preprocessor_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = self.fixture(root)
            tasks = []
            for index in range(260):
                source = root / f"f{index}.c"
                source.write_text(f"VALUE f{index}(void) {{ return 0; }}\n")
                tasks.append((source.stem, source, "us" if index % 2 else "eu", {}))
            calls = []
            with patch.object(declarations, "_preprocess", side_effect=self.cpp(batch, calls)):
                actual = list(declarations._source_units(batch, tasks))
            self.assertEqual([row for row, _ in actual], tasks)
            grouped = [text for text in calls if "__unbake_feedback_unit_" in text]
            self.assertEqual(len(grouped), 6)
            self.assertTrue(all(text.count("extern int __unbake_feedback_unit_") <= 129 for text in grouped))
            for task, text in actual:
                expected = "int" if task[2] == "us" else "float"
                self.assertIn(f"{expected} {task[0]}", text[1])


class DeclarationReuseTests(unittest.TestCase):
    def test_abstract_types_preserve_shape_and_do_not_copy_named_bodies(self):
        cases = [
            ("typedef int Word;", "int"),
            ("typedef const unsigned int *Word;", "const unsigned int *"),
            ("typedef int Word[4];", "int [4]"),
            ("typedef int (*Word)(int value);", "int (*)(int)"),
            ("typedef struct Big { int value; } *Word;", "struct Big *"),
            ("typedef union Big { int value; float other; } Word;", "union Big"),
            ("typedef struct { int value; } Word;", "struct { int value; }"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                node = declarations.c_parser.CParser().parse(source).ext[0].type
                before = copy.deepcopy(node)
                self.assertEqual(declarations._type(node), expected)
                self.assertEqual(
                    declarations.c_generator.CGenerator().visit(node),
                    declarations.c_generator.CGenerator().visit(before),
                )

        class Uncopied:
            def __deepcopy__(self, memo):
                raise AssertionError("copied discarded body")

        field = Uncopied()
        node = declarations.c_ast.Struct("Big", [field])
        self.assertEqual(declarations._type(node), "struct Big")
        self.assertEqual(node.decls, [field])

    def test_typedef_precedence_unknowns_and_shared_identity_match_reference(self):
        a = {"Word": "int", "Pointer": "Word *", "Unknown": "M2C_UNK32"}
        b = {"Word": "float", "Unknown": "long"}
        for maps in ([a] * 100, [a, a, b, a], [a, b, {"Word": "M2C_UNK"}], [dict(a), dict(a), b]):
            with self.subTest(maps=len(maps)):
                seeds = [{"aliases": row, "shared_typedefs": row} for row in maps]
                aliases = {name: value for row in maps for name, value in row.items()}
                expected = {
                    name: declarations.canonical(value, aliases)
                    for row in maps
                    for name, value in row.items()
                    if not declarations.unknown(value)
                }
                with patch.object(declarations, "canonical", wraps=declarations.canonical) as canonical:
                    self.assertEqual(solver._typedefs(seeds, aliases), expected)
                    self.assertEqual(canonical.call_count, len(expected))
        seeds = [{"aliases": a, "shared_typedefs": a} for _ in range(100)]
        with patch.object(declarations, "unknown", wraps=declarations.unknown) as unknown:
            solver._typedefs(seeds, a)
        self.assertEqual(unknown.call_count, len(a))

    def test_compact_receipts_share_only_immutable_header_typedefs(self):
        batch = declarations._PublishedDeclarations()
        prefix = "typedef int Word;\nstruct Shared { Word value; };\n"
        first = batch.extract((prefix, "Word alpha(void) {}"), {"function": "alpha"}, Path("alpha.c"), compact=True)
        second = batch.extract((prefix, "Word beta(void) {}"), {"function": "beta"}, Path("beta.c"), compact=True)
        self.assertIs(first["shared_typedefs"], second["shared_typedefs"])
        local = batch.extract((prefix, "typedef float Local; Local gamma(void) {}"), {}, Path("gamma.c"), compact=True)
        self.assertIsNot(first["aliases"], local["aliases"])
        self.assertNotIn("Local", first["aliases"])
        self.assertEqual(first["structs"]["Shared"]["provenance"]["function"], "alpha")
        self.assertEqual(second["structs"]["Shared"]["provenance"]["function"], "beta")

    def test_independent_summary_changes_are_refused_before_whole_program_work(self):
        for case in ("digest", "identity", "large_missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                project = SimpleNamespace(build=Path(tmp))
                directory = project.build / "types"
                directory.mkdir()
                database = directory / "database.json"
                database.write_bytes(b"database")
                if case == "large_missing":
                    with database.open("wb") as stream:
                        stream.truncate(65 * 1024 * 1024)
                else:
                    storage.write(
                        directory / "summary.json",
                        storage.encoded(
                            {
                                "schema": 1,
                                "project_id": "wrong" if case == "identity" else "same",
                                "database_sha256": "wrong" if case == "digest" else storage.file_digest(database),
                            }
                        ),
                    )
                with (
                    patch.object(storage, "identity", return_value={"schema": 1, "project_id": "same"}),
                    patch.object(declarations, "collect") as collect,
                    patch.object(solver, "infer") as infer,
                    patch("unbake.typemap.abi_facts.refine") as refine,
                    self.assertRaisesRegex(Held, "types.summary"),
                ):
                    solver.solve(project)
                collect.assert_not_called()
                infer.assert_not_called()
                refine.assert_not_called()
