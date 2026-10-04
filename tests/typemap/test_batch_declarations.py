"""Shared prefix reuse preserves independent source declarations and provenance."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.typemap import declarations, facts, storage


class BatchDeclarationsTests(unittest.TestCase):
    def test_array_address_abstract_declarators_parse(self):
        from pycparser import c_parser

        for spelling, expected in (
            ("int [] *", "int (*value)[]"),
            ("int [4][3] **", "int (**value)[4][3]"),
            ("int (*)[]", "int (*value)[]"),
            ("int *[4]", "int * value[4]"),
        ):
            with self.subTest(spelling=spelling):
                rendered = declarations.declarator(spelling, "value")
                self.assertEqual(rendered, expected)
                c_parser.CParser().parse("void function(" + rendered + ");")
                c_parser.CParser().parse("void function(" + declarations.declarator(spelling, "").strip() + ");")

    def test_admission_partitions_provider_failures_by_actual_imports(self):
        # CPP is the external boundary: simulate its token output, including macro
        # replay and a dependent source's bad declaration on only one VERSION.
        for mode in ("replay", "fallback", "pragma", "forced"):
            for reverse in (False, True):
                with self.subTest(mode=mode, reverse=reverse), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    include = root / "include"
                    include.mkdir()
                    (include / "types.h").write_text(
                        "#ifndef TYPES_H\n#define TYPES_H\ntypedef int Word;\n#endif\n"
                        + ("#pragma once\n" if mode == "pragma" else "")
                    )
                    (include / "unrelated.h").write_text("Missing authored;\n")
                    shared = include / "shared"
                    shared.mkdir()
                    (shared / "typemap.h").write_text("Missing unrelated;\n")
                    project = SimpleNamespace(
                        cppflags=("-P",),
                        root=root,
                        build=root / "build",
                        include=(include,),
                        default_compiler="cc",
                        compilers={
                            "cc": SimpleNamespace(
                                cflags=("-include", str(include / "unrelated.h")) if mode == "forced" else ()
                            )
                        },
                        version=lambda v: SimpleNamespace(macros=("VERSION_" + v,)),
                    )
                    policy = SimpleNamespace(cpp=Path("mock-cpp"), cores=2, cache_root=root / "cache")
                    entries = []
                    for name in ("good", "bad", "dependent", "other"):
                        path = root / (name + ".c")
                        path.write_text(
                            '#include "types.h"\n'
                            + (
                                '#include "shared/broken.h"\n'
                                if name == "bad"
                                else '#include "shared/transitive.h"\n'
                                if name == "dependent"
                                else ""
                            )
                            + f"Word {name}(Word value) {{return value;}}\n"
                        )
                        entries.append((name, path, ("us", "eu")))
                    if reverse:
                        entries.reverse()
                    inputs = []

                    def cpp(project, command, text, inputs=inputs, mode=mode, shared=shared, include=include):
                        inputs.append(text)
                        if "-dM" in command:
                            return "#define TYPES_H\n" if mode == "replay" else ""
                        prefix = "typedef int Word;\n" if str(include / "types.h") in text else ""
                        if str(include / "unrelated.h") in text or "-include" in command:
                            prefix += "Missing authored;\n"
                        if str(shared / "typemap.h") in text:
                            prefix += "Missing unrelated;\n"
                        boundary = declarations._BOUNDARY in text
                        path = Path(text.split('"')[-2] if boundary else text.split('"')[1])
                        content = path.read_text().replace("shared/transitive.h", "shared/broken.h")
                        if "shared/broken.h" in content and "-DVERSION_eu" in command:
                            content = "Missing provider;\n"
                        suffix = content.replace('#include "types.h"', "typedef int Word;").replace(
                            '#include "shared/broken.h"', ""
                        )
                        return prefix + declarations._BOUNDARY + "\n" + suffix if boundary else suffix

                    with patch.object(declarations, "_preprocess", side_effect=cpp):
                        refused = facts.refresh(
                            project, policy, [(name, path, v) for name, path, versions in entries for v in versions]
                        )
                    self.assertEqual(
                        set(refused),
                        {"good", "bad", "dependent", "other"} if mode == "forced" else {"bad", "dependent"},
                    )
                    self.assertTrue(all("types.declaration" in reason for reason in refused.values()))

    def compare(self, prefix, suffix, name):
        source = Path(name + ".c")
        provenance = {"kind": "proven", "function": name, "version": "us"}
        expected = declarations.extract(prefix + suffix, provenance, definitions=True, owned_source=source)
        declarations._portable_signatures(expected, declarations.extract(prefix, {})["aliases"])
        raw = prefix + declarations._BOUNDARY + "\n" + suffix
        actual = declarations.published(raw, provenance, source)
        expected["shared_typedefs"] = declarations.extract(prefix, {})["aliases"]
        self.assertEqual(storage.encoded(actual), storage.encoded(expected))
        return actual

    def test_line_markers_keep_owned_globals_and_exclude_header_globals(self):
        prefix = '# 1 "shared.h"\ntypedef int s32; extern s32 header_array[4];\n'
        suffix = '# 1 "alpha.c"\ns32 own_array[2]; s32 alpha(s32 x) { return x; }\n'
        actual = self.compare(prefix, suffix, "alpha")
        self.assertEqual(set(actual["globals"]), {"own_array"})

    def test_source_types_and_completed_header_aggregates_use_full_layout_context(self):
        prefix = "typedef int s32; struct Shared;\n"
        self.compare(prefix, "struct Shared { s32 value; }; s32 alpha(void) { return 0; }\n", "alpha")
        self.compare(prefix, "typedef s32 Local; Local beta(void) { return 1; }\n", "beta")
        self.compare(prefix, "s32 gamma(void) { return 2; }\n", "gamma")

    def test_changed_prefix_and_source_scopes_do_not_reuse_old_types(self):
        self.compare("typedef int Word;\n", "Word alpha(Word x) { int Word = x; return Word; }\n", "alpha")
        self.compare("typedef float Word;\n", "Word beta(Word x) { return x; }\n", "beta")
        self.compare("typedef int Word;\n", "Word gamma(Word x) { return x; }\n", "gamma")

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

    def test_local_alias_overrides_and_anonymous_externs_match_the_full_unit(self):
        prefix = (
            "typedef int s32; struct Handler { s32 id; }; typedef struct Handler Handler; "
            "struct Owner { Handler *entry; };\n"
        )
        for name in ("alpha", "beta"):
            self.compare(
                prefix,
                "typedef s32 (*Handler)(void *); extern struct { Handler callback; } table; "
                f"s32 {name}(void) {{ return 0; }}\n",
                name,
            )
        self.compare(
            prefix,
            "typedef struct Handler Handler; typedef Handler Other; s32 gamma(void) {return 1;}\n",
            "gamma",
        )

    def test_private_function_pointer_alias_is_emitted_without_a_private_typedef(self):
        prefix = "typedef int s32;\n"
        suffix = 'typedef s32 (*FuncPtr)(s32, s32, s32, s32);\nvoid dispatch(FuncPtr arg0) { __asm__("nop"); }\n'
        actual = self.compare(prefix, suffix, "dispatch")
        prototype = actual["functions"]["dispatch"]["prototype"]
        self.assertNotIn("FuncPtr", prototype)
        declarations.extract(prefix + prototype, {})
        self.assertRegex(prototype, r"\(\s*\*arg0\)")

    def test_width_placeholders_remain_unknown_when_their_header_carrier_is_expanded(self):
        prefix = "typedef int s32;\n"
        suffix = "typedef s32 M2C_UNK; M2C_UNK alpha(M2C_UNK arg0) { return arg0; }\n"
        actual = self.compare(prefix, suffix, "alpha")
        record = actual["functions"]["alpha"]
        self.assertEqual(record["return"], "M2C_UNK")
        self.assertEqual(record["params"][0]["type"], "M2C_UNK")
        self.assertNotIn("M2C_UNK", record["prototype"])
        declarations.extract(prefix + record["prototype"], {})
