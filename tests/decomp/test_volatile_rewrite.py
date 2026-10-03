"""Volatile normalization uses symbol evidence and private object proof."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.decomp import checks
from unbake.decomp import volatile_rewrite as rewrite
from unbake.layout.header_context import Headers
from unbake.project.config import Held


class VolatileRewriteTests(unittest.TestCase):
    def test_candidates_preserve_storage_and_remove_only_refused_tokens(self):
        cases = [
            ("local", "void f(void) { volatile int x; x=1; }", "void f(void) {          int x; x=1; }"),
            ("parameter", "void f(volatile int x) {}", "void f(         int x) {}"),
            ("field", "struct S { volatile int x; };", "struct S {          int x; };"),
            ("pointer itself", "void f(void) { int * volatile p; }", "void f(void) { int *          p; }"),
            ("aggregate cast", "x=((volatile struct S *)p)->x;", "x=((         struct S *)p)->x;"),
            ("macro argument", "x=FIELD(p, volatile float, 4);", "x=FIELD(p,          float, 4);"),
            ("alias", "typedef volatile unsigned int V;", "typedef          unsigned int V;"),
            ("global", "extern volatile int state;", None),
            ("array global", "extern volatile char data[];", None),
            ("function return", "volatile int f(void) {}", None),
            ("query", "int pad[sizeof(volatile int)];", None),
            ("device", "void f(void) { volatile int *p=(volatile int *)0xA4600010; *p=1; }", None),
            ("load cast", "x=*(volatile int *)&p->field;", None),
            ("query and storage", "volatile int x[sizeof(volatile int)];", "         int x[sizeof(volatile int)];"),
            ("comment and string", '/* volatile int x; */ char *s="volatile";', None),
            ("explicit exception", "/* FAKEMATCH: measured order */ volatile int x;", None),
        ]
        for name, source, expected in cases:
            with self.subTest(name=name):
                actual, _ = rewrite.candidate(source)
                self.assertEqual(actual, source if expected is None else expected)
                self.assertEqual(len(actual), len(source))
                self.assertEqual(actual.count("\n"), source.count("\n"))

    def test_externs_are_objects_at_file_scope(self):
        cases = [
            ("extern volatile int a, b;", ["extern volatile int a, b;"]),
            ("extern int x; extern volatile int y;", ["extern volatile int y;"]),
            ("void f(void) { extern volatile int x; }", []),
            ("extern volatile int f(void);", []),
            ("extern volatile int x=1;", []),
            ("#if X\nextern volatile int x;\n#endif", []),
            ("/* extern volatile int x; */", []),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual([source[a:b] for a, b in rewrite.externs(source)], expected)

    def test_injected_shared_evidence_is_not_normalized(self):
        prefix = "extern volatile int shared;\n"
        source = prefix + rewrite.BOUNDARY + "void f(void) { volatile int x; }"
        actual, _ = rewrite.candidate(source)
        self.assertTrue(actual.startswith(prefix + rewrite.BOUNDARY))
        self.assertNotIn("volatile", actual.partition(rewrite.BOUNDARY)[2])

    def test_proof_is_required_and_authored_inputs_are_unchanged(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            source = "void f(void) { volatile int x; x=1; }"
            unit = root / "f.c"
            unit.write_text(source)
            header = root / "include/types.h"
            headers = Headers({header: "typedef int Scalar;\n"}, root=root)
            project = SimpleNamespace(root=root)
            expected, _ = rewrite.candidate(source)
            with (
                patch("unbake.decomp.work.overlay", return_value=project),
                patch("unbake.decomp.gbi_proof.preserve") as proof,
            ):
                actual, evidence = rewrite.proven(project, None, unit, source, headers, ("us", "eu"))
                self.assertEqual(actual, expected)
                self.assertFalse(evidence)
                proof.assert_called_once_with(project, None, unit, source, expected)
                proof.side_effect = Held("gbi", "VERSION eu NON_MATCHING=1: rewrite changes codegen")
                with self.assertRaisesRegex(Held, "volatile int x.*VERSION eu NON_MATCHING=1"):
                    rewrite.proven(project, None, unit, source, headers, ("us", "eu"))
            self.assertEqual(unit.read_text(), source)
            self.assertEqual(headers.texts[header], "typedef int Scalar;\n")
            self.assertFalse(header.exists())

    def test_plain_and_device_sources_do_not_compile_for_rewrite(self):
        for source in (
            "void f(void) {}",
            "typedef int T;\n" + rewrite.BOUNDARY + "void f(void) {}",
            "void f(void) { volatile int *p=(volatile int *)0xA4600010; }",
        ):
            with self.subTest(source=source), patch("unbake.decomp.gbi_proof.preserve") as proof:
                actual, evidence = rewrite.proven(
                    None, None, Path("f.c"), source, Headers({}, root=Path("project")), ("us",)
                )
                self.assertEqual(actual, source)
                self.assertFalse(evidence)
                proof.assert_not_called()

    def test_globals_move_to_evidence_only_after_symbol_validation(self):
        source = "extern volatile int shared;\nvoid f(void) { shared=1; }"
        project = SimpleNamespace(root=Path("/project"))
        for prefix in ("", "typedef int Domain;\n" + rewrite.BOUNDARY):
            with (
                self.subTest(prefix=prefix),
                patch("unbake.typemap.declaration_evidence.validate_symbols") as validate,
                patch("unbake.decomp.gbi_proof.preserve") as proof,
            ):
                actual, evidence = rewrite.proven(
                    project, None, Path("f.c"), prefix + source, Headers({}, root=Path("project")), ("us",)
                )
                self.assertTrue(evidence)
                before, marker, body = actual.partition(rewrite.BOUNDARY)
                self.assertEqual(marker, rewrite.BOUNDARY)
                self.assertIn("extern volatile int shared;", before)
                self.assertNotIn("volatile", body)
                self.assertIn("shared=1;", body)
                validate.assert_called_once()
                self.assertEqual(validate.call_args.args[1][0].names, frozenset({"shared"}))
                proof.assert_not_called()

    def test_global_conflicts_and_missing_symbols_hold_the_exact_declaration(self):
        source = "extern volatile int shared;\nvoid f(void) { shared=1; }"
        cases = [
            ({}, Held("types", "absent live symbol/address inventory"), "absent live symbol"),
            ({Path("types.h"): "extern int shared;"}, None, "shared declaration conflict"),
            ({Path("types.h"): "extern volatile short shared;"}, None, "shared declaration conflict"),
        ]
        for contents, failure, reason in cases:
            with (
                self.subTest(reason=reason),
                patch("unbake.typemap.declaration_evidence.validate_symbols", side_effect=failure),
            ):
                with self.assertRaises(Held) as caught:
                    rewrite.proven(None, None, Path("f.c"), source, Headers(contents, root=Path("project")), ("us",))
                self.assertEqual(caught.exception.phase, "volatile")
                self.assertIn("extern volatile int shared;", caught.exception.reason)
                self.assertIn(reason, caught.exception.reason)

    def test_equal_shared_declaration_and_multiple_objects_are_retained(self):
        source = "extern volatile int shared; extern volatile int a,b; void f(void) { shared=a+b; }"
        with patch("unbake.typemap.declaration_evidence.validate_symbols") as validate:
            actual, evidence = rewrite.proven(
                None,
                None,
                Path("f.c"),
                source,
                Headers({Path("types.h"): "extern volatile int shared;"}, root=Path("project")),
                ("us",),
            )
        self.assertTrue(evidence)
        self.assertEqual(validate.call_count, 2)
        self.assertIn("extern volatile int a,b;", actual.partition(rewrite.BOUNDARY)[0])
        self.assertIn("shared=a+b;", actual.partition(rewrite.BOUNDARY)[2])

    def test_refusal_reports_original_line_and_accepted_form(self):
        findings = checks.run("\nvoid f(void) { volatile int local; }")
        finding = next(f for f in findings if f.rule == "volatile-storage")
        self.assertEqual(finding.line, 2)
        self.assertIn("volatile int local", finding.text)
        self.assertIn("accepted form:", finding.text)

    def test_object_proof_requires_every_version_and_both_modes(self):
        from unbake.decomp import gbi_proof

        project = SimpleNamespace(versions=("us", "eu"))
        unit = Path("f.c")
        for failure in (None, ("us", 0), ("us", 1), ("eu", 0), ("eu", 1)):
            calls = []

            def code(project, policy, version, path, mode, calls=calls, failure=failure):
                calls.append((version, path.parent.name, mode))
                return 1 if path.parent.name == "after" and failure == (version, mode) else 0

            with (
                self.subTest(failure=failure),
                patch("unbake.layout.split.functions", return_value=[SimpleNamespace(aliases=("f",))]),
                patch.object(gbi_proof, "code", side_effect=code),
            ):
                if failure is None:
                    gbi_proof.preserve(project, None, unit, "before", "after")
                    self.assertEqual(
                        calls,
                        [(v, side, m) for v in ("us", "eu") for m in (0, 1) for side in ("before", "after")],
                    )
                else:
                    with self.assertRaisesRegex(Held, f"VERSION {failure[0]} NON_MATCHING={failure[1]}"):
                        gbi_proof.preserve(project, None, unit, "before", "after")

    def test_volatile_imports_use_only_equivalent_scalars_and_keep_sdk_choice(self):
        from unbake.decomp import gbi_recover

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            old = root / "old"
            old.mkdir()
            live = root / "include"
            legacy = old / "scalars.h"
            (old / "commands.h").write_text("#define gDPSync(p) legacy(p)\n")
            project = SimpleNamespace(include=(live,), declaration_evidence=(old,))
            headers = {live / "types.h": "typedef signed int s32;\n"}
            cases = [
                ("equivalent", "typedef int s32;", "", True),
                ("incompatible", "typedef unsigned int s32;", "", False),
                ("authored struct", "typedef int s32; struct S {int x;};", "", False),
                ("needed macro", "typedef int s32;\n#define NULL 0\n", "x=NULL;", False),
                ("local macro", "typedef int s32;\n#define NULL 0\n", "#define NULL ((void *)0)\nx=NULL;", True),
            ]
            for name, evidence, extra, mapped in cases:
                with self.subTest(name=name):
                    legacy.write_text(evidence)
                    source = '#include "scalars.h"\n#include "commands.h"\n' + extra + "\nvolatile s32 x;"
                    after = gbi_recover.import_aliases(
                        project, source, headers, sdk_aliases=False, rules=frozenset({"volatile-storage"})
                    )
                    self.assertEqual('#include "types.h"' in after, mapped)
                    self.assertIn('#include "commands.h"', after)
                    self.assertIn("volatile s32 x;", after)

    def test_unresolved_imports_are_still_refused_after_folding(self):
        from unbake.match import batch_fold

        source = '#include "../unresolved.h"\nvoid f(void) {}'
        candidate = SimpleNamespace(function="f", content=source.encode(), versions=("us",))
        with patch("unbake.match.declarations.fold_source", return_value=SimpleNamespace(source=source)):
            with self.assertRaisesRegex(Held, "local-include.*unresolved.h"):
                batch_fold._folded(None, None, None, candidate)
