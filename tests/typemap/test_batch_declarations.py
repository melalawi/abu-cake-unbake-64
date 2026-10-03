"""Shared prefix reuse preserves independent source declarations and provenance."""

import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.project.config import Held
from unbake.typemap import declarations, storage


class BatchDeclarationsTests(unittest.TestCase):
    def test_failed_prefix_is_parsed_once_without_retaining_exception_frames(self):
        prefix = "typedef Missing Missing;\n"
        with self.assertRaises(Held) as expected:
            declarations.extract(prefix, {}, definitions=True, owned_source=Path("__unbake_header_prefix__"))
        batch = declarations._PublishedDeclarations()
        errors = []
        with patch.object(
            declarations.c_parser.CParser, "parse", autospec=True, side_effect=declarations.c_parser.CParser.parse
        ) as parse:
            for name in ("alpha", "beta", "gamma"):
                with self.assertRaises(Held) as actual:
                    batch.extract((prefix, f"int {name}(void) {{ return 0; }}"), {"function": name}, Path(name + ".c"))
                self.assertEqual(
                    (actual.exception.phase, actual.exception.reason),
                    (expected.exception.phase, expected.exception.reason),
                )
                errors.append(actual.exception)
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(len({id(error) for error in errors}), 3)
        self.assertEqual(batch.prefix_errors, {prefix: (expected.exception.phase, expected.exception.reason)})
        # A different (repaired) prefix remains eligible in the same batch.
        actual = batch.extract(("typedef int Missing;\n", "Missing delta(void) { return 1; }"), {}, Path("delta.c"))
        self.assertIn("delta", actual["functions"])

    def test_source_declaration_errors_do_not_poison_a_valid_prefix(self):
        batch = declarations._PublishedDeclarations()
        prefix = "typedef int Word;\n"
        with self.assertRaises(Held):
            batch.extract((prefix, "Missing alpha(void);"), {}, Path("alpha.c"))
        actual = batch.extract((prefix, "Word beta(void) { return 1; }"), {}, Path("beta.c"))
        self.assertIn("beta", actual["functions"])
        self.assertEqual(batch.prefix_errors, {})

    def compare(self, batch, prefix, suffix, name):
        source = Path(name + ".c")
        provenance = {"kind": "proven", "function": name, "version": "us"}
        expected = declarations.extract(prefix + suffix, provenance, definitions=True, owned_source=source)
        declarations._portable_signatures(expected, declarations.extract(prefix, {})["aliases"])
        raw = prefix + declarations._BOUNDARY + "\n" + suffix
        actual = batch.extract(raw, provenance, source)
        expected["shared_typedefs"] = declarations.extract(prefix, {})["aliases"]
        self.assertEqual(storage.encoded(actual), storage.encoded(expected))
        return actual

    def test_prefix_parses_once_and_each_source_gets_its_own_provenance(self):
        prefix = "typedef int s32; struct Shared { s32 count; };\n"
        batch = declarations._PublishedDeclarations()
        with patch.object(
            declarations.c_parser.CParser, "parse", autospec=True, side_effect=declarations.c_parser.CParser.parse
        ) as parse:
            # Build reference facts outside the counted calls.
            raw = prefix + declarations._BOUNDARY + "\ns32 alpha(s32 x) { return x; }\n"
            first = batch.extract(raw, {"kind": "proven", "function": "alpha"}, Path("alpha.c"))
            second = batch.extract(raw, {"kind": "proven", "function": "beta"}, Path("beta.c"))
            self.assertEqual(parse.call_count, 2)
        self.assertEqual(first["structs"]["Shared"]["provenance"]["function"], "alpha")
        self.assertEqual(second["structs"]["Shared"]["provenance"]["function"], "beta")

    def test_line_markers_keep_owned_globals_and_exclude_header_globals(self):
        prefix = '# 1 "shared.h"\ntypedef int s32; extern s32 header_array[4];\n'
        suffix = '# 1 "alpha.c"\ns32 own_array[2]; s32 alpha(s32 x) { return x; }\n'
        actual = self.compare(declarations._PublishedDeclarations(), prefix, suffix, "alpha")
        self.assertEqual(set(actual["globals"]), {"own_array"})

    def test_source_types_and_completed_header_aggregates_use_full_layout_context(self):
        prefix = "typedef int s32; struct Shared;\n"
        batch = declarations._PublishedDeclarations()
        self.compare(batch, prefix, "struct Shared { s32 value; }; s32 alpha(void) { return 0; }\n", "alpha")
        self.compare(batch, prefix, "typedef s32 Local; Local beta(void) { return 1; }\n", "beta")
        self.compare(batch, prefix, "s32 gamma(void) { return 2; }\n", "gamma")

    def test_changed_prefix_and_source_scopes_do_not_reuse_old_types(self):
        batch = declarations._PublishedDeclarations()
        self.compare(batch, "typedef int Word;\n", "Word alpha(Word x) { int Word = x; return Word; }\n", "alpha")
        self.compare(batch, "typedef float Word;\n", "Word beta(Word x) { return x; }\n", "beta")
        self.compare(batch, "typedef int Word;\n", "Word gamma(Word x) { return x; }\n", "gamma")

    def test_proven_gcc_dispatch_body_does_not_enter_declaration_parser(self):
        source = """typedef int s32;
void dispatch(void *state) {
    static void *keep_labels[0] __attribute__((section(".sdata"))) = { &&case_0 };
    goto *keep_labels[(s32)state];
case_0:
    __asm__("nop");
}
extern s32 resident __attribute__((section(".sdata")));
"""
        seed = declarations.extract(
            '# 1 "dispatch.c"\n' + source, {"kind": "proven"}, definitions=True, owned_source=Path("dispatch.c")
        )
        self.assertEqual(seed["functions"]["dispatch"]["prototype"], "void dispatch(void *state);")
        self.assertEqual(set(seed["globals"]), {"resident"})
        self.assertNotIn("keep_labels", seed["globals"])

    def test_function_erasure_preserves_aggregates_and_compound_initializers(self):
        source = """struct Value { int field; };
struct Value resident = (struct Value){ 3 };
int (*factory(void))(int) { return 0; }
"""
        seed = declarations.extract(source, {})
        self.assertEqual(seed["structs"]["Value"]["size"], 4)
        self.assertEqual(seed["globals"]["resident"]["type"], "struct Value")
        self.assertIn("factory", seed["functions"])

    def test_compact_layout_runs_keep_last_provenance_and_all_conflicts(self):
        from unbake.typemap.solver import Constraints, _merge_records

        template = {"Shared": {"size": 4, "fields": [], "provenance": {}}}
        shared = [declarations.ProvenStructs(template, {"kind": "proven", "function": str(i)}) for i in range(20)]
        for initial in ({}, {"Shared": {"size": 8, "fields": [], "provenance": {"kind": "proven"}}}):
            compact = [{"structs": initial}, *({"structs": row} for row in shared)]
            ordinary = [{"structs": initial}, *({"structs": dict(row)} for row in shared)]
            old, new = Constraints(), Constraints()
            self.assertEqual(_merge_records(ordinary, "structs", old), _merge_records(compact, "structs", new))
            self.assertEqual(old.facts, new.facts)

    def test_macro_replay_skips_consumed_guards_and_preserves_source_coordinates(self):
        import os
        import tempfile
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as tmp:
            root = Path(tmp)
            include = root / "include"
            include.mkdir()
            header = include / "types.h"
            header.write_text("#ifndef TYPES_H\n#define TYPES_H\ntypedef int Word;\n#endif\n")
            source = root / "alpha.c"
            original = '#include "types.h"\nWord alpha(void) { return SCALE; }\n'
            source.write_text(original)
            project = SimpleNamespace(
                root=root,
                include=(include,),
                default_compiler="cc",
                compilers={"cc": SimpleNamespace(cflags=())},
                version=lambda v: SimpleNamespace(macros=("VERSION_US=1",)),
            )
            policy = SimpleNamespace(cpp=Path("mock-cpp"), cppflags=("-P",), cores=1)
            transformed = []

            def cpp(project, command, text):
                if "-dM" in command:
                    return "#define TYPES_H\n#define SCALE 3\n"
                if declarations._BOUNDARY in text:
                    return "typedef int Word;\n" + declarations._BOUNDARY + "\nWord alpha(void) { return 3; }\n"
                path = Path(text.split('"')[1])
                content = path.read_text()
                transformed.append(content)
                return content.replace("SCALE", "3")

            with patch.object(declarations, "_preprocess", side_effect=cpp) as preprocess:
                headers = declarations._PublishedHeaders(project, policy, root)
                batch = declarations._PublishedDeclarations()
                for _ in range(2):
                    actual = batch.extract(headers.source("us", source), {}, source)
                    self.assertEqual(actual["functions"]["alpha"]["return"], "Word")
                self.assertEqual(preprocess.call_count, 4)
            self.assertTrue(all(content.startswith(f'#line 1 "{source}"') for content in transformed))
            self.assertTrue(all('#include "types.h"' not in content for content in transformed))
            self.assertEqual(source.read_text(), original)

    def test_outer_guard_else_and_stateful_pragmas_keep_their_preprocessor_effects(self):
        import os
        import tempfile
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as tmp:
            root = Path(tmp)
            header = root / "types.h"
            header.write_text(
                "#ifndef TYPES_H\n#define TYPES_H\ntypedef int Word;\n#else\n#error repeated include\n#endif\n"
            )
            project = SimpleNamespace(
                root=root,
                include=(root,),
                default_compiler="cc",
                compilers={"cc": SimpleNamespace(cflags=())},
                version=lambda v: SimpleNamespace(macros=()),
            )
            policy = SimpleNamespace(cpp=Path("mock-cpp"), cppflags=())
            headers = declarations._PublishedHeaders(project, policy, root)
            self.assertNotIn(header, headers.guarded)
            header.write_text("#pragma once\ntypedef int Word;\n")
            headers = declarations._PublishedHeaders(project, policy, root)
            self.assertFalse(headers.replay)

    def test_local_alias_overrides_and_anonymous_externs_match_the_full_unit(self):
        prefix = (
            "typedef int s32; struct Handler { s32 id; }; typedef struct Handler Handler; "
            "struct Owner { Handler *entry; };\n"
        )
        batch = declarations._PublishedDeclarations()
        for name in ("alpha", "beta"):
            self.compare(
                batch,
                prefix,
                "typedef s32 (*Handler)(void *); extern struct { Handler callback; } table; "
                f"s32 {name}(void) {{ return 0; }}\n",
                name,
            )
        self.compare(
            batch,
            prefix,
            "typedef struct Handler Handler; typedef Handler Other; s32 gamma(void) {return 1;}\n",
            "gamma",
        )

    def test_private_function_pointer_alias_is_emitted_without_a_private_typedef(self):
        prefix = "typedef int s32;\n"
        suffix = 'typedef s32 (*FuncPtr)(s32, s32, s32, s32);\nvoid dispatch(FuncPtr arg0) { __asm__("nop"); }\n'
        actual = self.compare(declarations._PublishedDeclarations(), prefix, suffix, "dispatch")
        prototype = actual["functions"]["dispatch"]["prototype"]
        self.assertNotIn("FuncPtr", prototype)
        declarations.extract(prefix + prototype, {})
        self.assertRegex(prototype, r"\(\s*\*arg0\)")

    def test_width_placeholders_remain_unknown_when_their_header_carrier_is_expanded(self):
        prefix = "typedef int s32;\n"
        suffix = "typedef s32 M2C_UNK; M2C_UNK alpha(M2C_UNK arg0) { return arg0; }\n"
        actual = self.compare(declarations._PublishedDeclarations(), prefix, suffix, "alpha")
        record = actual["functions"]["alpha"]
        self.assertEqual(record["return"], "M2C_UNK")
        self.assertEqual(record["params"][0]["type"], "M2C_UNK")
        self.assertNotIn("M2C_UNK", record["prototype"])
        declarations.extract(prefix + record["prototype"], {})

    def test_source_receipt_cache_is_bounded_and_eviction_preserves_bytes(self):
        batch = declarations._PublishedDeclarations()
        prefix = "typedef int Word;\n"
        first = None
        for i in range(20):
            name = f"unit_{i}"
            result = batch.extract(
                (prefix, f"Word {name}(void) {{ return {i}; }}"), {}, Path(name + ".c"), compact=False
            )
            if i == 0:
                first = storage.encoded(result)
            self.assertLessEqual(len(batch.sources), 8)
        again = batch.extract((prefix, "Word unit_0(void) { return 0; }"), {}, Path("unit_0.c"), compact=False)
        self.assertEqual(storage.encoded(again), first)

    def test_preprocessor_queue_retains_only_one_unit_per_worker(self):
        import tempfile
        from types import SimpleNamespace

        for cores in (1, 12):
            with self.subTest(cores=cores), tempfile.TemporaryDirectory() as directory:
                sizes = []

                class Pool:
                    def __init__(self, cores=cores, **kwargs):
                        self.workers = kwargs["max_workers"]
                        self.asserted_workers = min(2, cores)

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        pass

                    def map(self, fn, tasks, sizes=sizes):
                        sizes.append(len(tasks))
                        return map(fn, tasks)

                headers = SimpleNamespace(
                    scratch=Path(directory), policy=SimpleNamespace(cores=cores), source=lambda v, p: str(p)
                )
                tasks = [(str(i), Path(str(i)), "us", {}) for i in range(25)]
                with patch.object(declarations, "ThreadPoolExecutor", Pool):
                    results = list(declarations._source_units(headers, tasks))
                self.assertEqual([task[0] for task, text in results], [str(i) for i in range(25)])
                self.assertLessEqual(max(sizes), min(2, cores))

    def test_distinct_header_contexts_are_bounded_and_eviction_preserves_declarations(self):
        batch = declarations._PublishedDeclarations()
        expected = None
        for i in range(12):
            prefix = f"typedef int Word_{i};\n"
            suffix = f"Word_{i} unit_{i}(void) {{ return {i}; }}"
            result = batch.extract((prefix, suffix), {}, Path(f"unit_{i}.c"))
            if i == 0:
                expected = storage.encoded(result)
            self.assertLessEqual(len(batch.prefixes), 4)
        actual = batch.extract(("typedef int Word_0;\n", "Word_0 unit_0(void) { return 0; }"), {}, Path("unit_0.c"))
        self.assertEqual(storage.encoded(actual), expected)
