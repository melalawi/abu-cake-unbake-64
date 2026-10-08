"""Real address receipts are repaired before native comparison and publication."""

import copy
import hashlib
import json
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import cdecl, pool, runner
from unbake.config import Held
from unbake.fold import declarations as fold_declarations
from unbake.layout import header_step, index, redeclarations, split
from unbake.layout.header_context import Headers
from unbake.typemap import database, namespace, regeneration, types_db
from unbake.work import compare

FIXTURE = Path(__file__).parents[1] / "fixtures/function_address"
NAMES = {"func_8011F810", "func_8011FEBC"}
SOURCE = (FIXTURE / "consumer.c").read_text()
HEADER = (FIXTURE / "objects.h").read_text()
VALUE = {"function_symbols": sorted(NAMES), "functions": {name: {"state": "unknown"} for name in NAMES}}


def canonical(case, text):
    for name in NAMES:
        case.assertNotIn("extern s32 " + name + ";", text)
        case.assertRegex(text, r"\b" + name + r"\s*\(")


class FunctionAddressDeclarations(unittest.TestCase):
    def test_real_receipts_share_two_parses_and_one_scan_per_distinct_payload(self):
        contents = {Path(f"receipt{i}.h"): HEADER for i in range(8)}
        with (
            patch.object(cdecl, "parse", wraps=cdecl.parse) as parse,
            patch.object(redeclarations, "spans", wraps=redeclarations.spans) as scan,
        ):
            contracts = namespace.FunctionDeclarations(VALUE, contents)
            headers = [contracts.rewrite(text) for text in contents.values()]
            source = contracts.rewrite(SOURCE)
            for _ in range(8):
                self.assertEqual(contracts.rewrite(SOURCE), source)
            self.assertEqual(parse.call_count, 2)
            self.assertEqual(scan.call_count, 2)
        self.assertEqual(len(contracts.parsed), 2)
        self.assertEqual(len(contracts.rewritten), 2)
        canonical(self, source)
        canonical(self, headers[0])
        # The address expressions and complete implementation are byte-identical.
        self.assertEqual(source[source.index("void func_8011E564(") :], SOURCE[SOURCE.index("void func_8011E564(") :])
        namespace.check(VALUE, {Path("canonical.h"): headers[0]})

    def test_existing_contract_wins_over_scalar_and_unknown_parameter_list(self):
        prototype = "void func_8011F810(void *arg, int value);"
        contracts = namespace.FunctionDeclarations(VALUE, {Path("object.h"): HEADER, Path("contract.h"): prototype})
        after = contracts.rewrite(SOURCE)
        self.assertIn(prototype, after)
        self.assertIn("extern s32 func_8011FEBC();", after)
        self.assertEqual(contracts.rewrite(prototype), prototype)

    def test_solved_contract_is_used_without_turning_its_pointer_into_storage(self):
        value = copy.deepcopy(VALUE)
        prototype = "float func_8011F810(double arg);"
        value["functions"]["func_8011F810"]["prototype"] = prototype
        after = namespace.FunctionDeclarations(value, {}).rewrite(SOURCE)
        self.assertIn(prototype, after)
        self.assertIn("(s32) &func_8011F810", after)

    def test_real_objects_and_function_typedefs_keep_their_categories(self):
        text = (
            "typedef int Entry(int); typedef Entry Alias; extern Alias func_8011F810;\n"
            "extern int (*callback)(int); extern int func_looks_like_code;\n"
            "extern int data[2]; struct func_8011F810 { int field; };"
        )
        contracts = namespace.FunctionDeclarations(VALUE, {Path("contracts.h"): text})
        self.assertEqual(contracts.rewrite(text), text)
        namespace.check(VALUE, {Path("contracts.h"): text})
        extracted = cdecl.declarations(text)
        self.assertIn("callback", extracted.declared)
        self.assertIn("func_looks_like_code", extracted.declared)

    def test_mixed_object_statement_preserves_unrelated_storage_and_expression(self):
        text = "extern s32 func_8011F810, actual_object;\nvoid use(void) { sink((s32) &func_8011F810); }"
        after = namespace.FunctionDeclarations(VALUE, {}).rewrite(text)
        self.assertIn("extern s32 func_8011F810();", after)
        self.assertIn("extern s32 actual_object;", after)
        self.assertTrue(after.endswith(text[text.index("void use") :]))

    def test_initialized_and_derived_objects_remain_explicit_conflicts(self):
        for text in (
            "extern s32 func_8011F810 = 1;",
            "extern s32 func_8011F810[2];",
            "extern s32 (*func_8011F810)(int);",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(Held, "headers.namespace.*function identity"):
                namespace.FunctionDeclarations(VALUE, {}).rewrite(text)
        with self.assertRaises(Held):
            namespace.check({**VALUE, "globals": {"func_8011F810": {}}}, {})

    def test_publication_preserves_rendered_changes_and_repairs_each_consumer_once(self):
        header, source, untouched = Path("code.h"), Path("consumer.c"), Path("untouched.c")
        contracts = namespace.FunctionDeclarations(VALUE, {header: HEADER})
        rendered = HEADER.replace("#endif", "extern int real_object;\n#endif")
        changes = namespace.publication_outputs(
            contracts, {header: HEADER}, {source: SOURCE, untouched: "int untouched;"}, {header: rendered.encode()}
        )
        self.assertEqual(set(changes), {header, source})
        self.assertIn(b"extern int real_object;", changes[header])
        canonical(self, changes[header].decode())
        canonical(self, changes[source].decode())
        self.assertEqual(namespace.publication_outputs(contracts, {}, {source: changes[source].decode()}, {}), {})


class FunctionAddressPublication(ProjectCase):
    def setUp(self):
        super().setUp()
        self.header = self.project.include[0] / "span_1000/code_8011F7F0.h"
        self.header.parent.mkdir()
        self.header.write_text(HEADER)
        self.argument = self.header.parent / "code_8011E3F0.h"
        self.argument.write_bytes((FIXTURE / "argument.h").read_bytes())
        types = self.project.include[0] / "types.h"
        types.write_text(types.read_text() + "typedef signed char s8;\n")
        self.source = self.project.src / "alpha.c"
        self.source.write_text(SOURCE.replace("void func_8011E564(", "void alpha("))
        for version in self.versions:
            symbols = self.project.version(version).symbols
            symbols.write_text(symbols.read_text() + (FIXTURE / "symbols.txt").read_text())
            layout = self.project.version(version).split
            layout.write_text(layout.read_text().replace("asm, alpha]", "c, alpha]"))
        lookup = {
            "schema": 1,
            "clusters": {},
            "headers": {
                path.relative_to(self.project.include[0]).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.header, self.argument)
            },
            "symbols": {},
        }
        index.path(self.project).parent.mkdir(parents=True, exist_ok=True)
        index.path(self.project).write_bytes(index.encoded(lookup))

    def value(self):
        value = {kind: {} for kind in ("structs", "globals", "arrays", "dependencies")}
        value.update(copy.deepcopy(VALUE), revision=1)
        return value

    def test_inventory_annotations_are_identity_with_one_metadata_pass_per_version(self):
        body = split.functions(self.project, "us")[0]
        with (
            patch.object(split, "functions", return_value=[body]) as functions,
            patch.object(split, "symbols", wraps=split.symbols) as symbols,
            patch.object(split, "words", side_effect=AssertionError("ROM body read")) as words,
            patch.object(types_db, "entries", side_effect=AssertionError("missing database read")) as records,
        ):
            contracts = namespace.project_declarations(self.project, {self.header: HEADER}, texts=(SOURCE,))
        self.assertEqual(contracts.names, NAMES)
        self.assertEqual(functions.call_count, len(self.versions))
        self.assertEqual(symbols.call_count, len(self.versions))
        self.assertEqual(words.call_count, 0)
        self.assertEqual(records.call_count, 0)
        canonical(self, contracts.rewrite(SOURCE))

    def test_generated_snapshot_reads_each_header_once_per_session(self):
        reads = []
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            reads.append(path)
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            session = regeneration.Session(self.project, None)
        self.assertEqual(reads.count(self.header), 1)
        self.assertEqual(reads.count(self.argument), 1)
        self.assertEqual(reads.count(self.source), 1)
        self.assertIn(self.header, session.installed)

    def test_renderer_canonicalizes_retained_function_objects_without_writing(self):
        value = self.value()
        before = {path: path.read_bytes() for path in (self.header, self.source)}
        session = regeneration.Session(self.project, None)
        outputs = database._render(self.project, value, None, session)
        bodies = "\n".join(text.decode() for path, text in outputs.items() if path.suffix == ".h")
        canonical(self, bodies)
        namespace.check(value, {Path(path): text for path, text in value["published_declarations"].items()})
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def publish(self, *, fail=False, policy=True):
        before = {path: path.read_bytes() for path in (self.header, self.source)}
        builds = []

        def build(view, host, unit, version, *, source):
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            canonical(self, source.read_text())
            canonical(self, (view.work_include[0] / self.header.relative_to(self.project.include[0])).read_text())
            builds.append((unit, version))
            data = split.words(self.project, split.functions(self.project, version)[0])
            return b"changed" if fail else data

        def run(host, function, jobs, shared=None):
            return [function(job) if shared is None else function(shared, job) for job in jobs]

        session = regeneration.Session(self.project, None)
        with (
            patch.object(regeneration, "Session", return_value=session),
            patch.object(database, "validate_headers") as validate_headers,
            patch.object(pool, "run", side_effect=run),
            patch.object(runner, "build_unit", side_effect=build) as native,
            patch.object(header_step, "validate", wraps=header_step.validate) as validate,
            patch.object(types_db, "install", wraps=types_db.install) as install,
        ):
            if fail or not policy:
                with self.assertRaises(Held):
                    database.publish(self.project, self.value(), {}, policy=self.host if policy else None)
            else:
                database.publish(self.project, self.value(), {}, policy=self.host)
        self.assertEqual(validate_headers.call_count, 1)
        self.assertEqual(native.call_count, len(self.versions) if policy else 0)
        self.assertEqual(validate.call_count, 1 if policy else 0)
        if policy:
            self.assertIs(validate.call_args.kwargs["prove_all"], True)
            self.assertEqual(builds, [("alpha", version) for version in self.versions])
        self.assertEqual(install.call_count, 0 if fail or not policy else 1)
        if fail or not policy:
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertFalse(types_db.path(self.project).exists())
        else:
            canonical(self, self.source.read_text())
            canonical(self, self.header.read_text())
            lookup = json.loads(index.path(self.project).read_bytes())
            self.assertEqual(
                lookup["headers"][self.header.relative_to(self.project.include[0]).as_posix()],
                hashlib.sha256(self.header.read_bytes()).hexdigest(),
            )

    def test_recompute_requires_exact_native_proofs_on_each_holding_version_before_install(self):
        self.publish()

    def test_native_difference_refuses_the_whole_publication_before_any_write(self):
        self.publish(fail=True)

    def test_repair_without_native_host_refuses_before_install(self):
        self.publish(policy=False)

    def test_compare_compiles_canonical_draft_and_headers_once_per_holding_version(self):
        seen = []

        @contextmanager
        def compile_unit(view, host, source, version, **kwargs):
            canonical(self, source.read_text())
            canonical(self, (view.work_include[0] / self.header.relative_to(self.project.include[0])).read_text())
            seen.append((source, version))
            self.assertEqual(self.header.read_text(), HEADER)
            yield self.root / "native.o"

        def link(project, host, obj, version, row, source, capture_info=None):
            return split.words(project, row), []

        with (
            patch("unbake.fold.provider_reuse.view", side_effect=lambda project, *args: nullcontext(project)),
            patch.object(runner, "compile_unit", side_effect=compile_unit) as native,
            patch.object(runner, "link_function", side_effect=link) as linked,
        ):
            measured = compare.measure(self.project, self.host, self.source)
        # The mock link supplies no native digest chain, so strict exactness is out of scope here: every
        # holding version's words must match the target.
        for version, compared in measured.compares.items():
            with self.subTest(version=version):
                self.assertEqual(compared.identical_words, compared.target_words)
                self.assertFalse(any(compared.typed.values()))
        self.assertEqual(set(measured.compares), set(self.versions))
        self.assertEqual(native.call_count, len(self.versions))
        self.assertEqual(linked.call_count, len(self.versions))
        self.assertEqual([version for _path, version in seen], list(self.versions))
        self.assertEqual(len({path for path, _version in seen}), 1)
        self.assertTrue(all(not path.exists() for path, _version in seen))
        self.assertIn("extern s32 func_8011F810;", self.source.read_text())

    def test_fold_canonicalizes_source_and_injected_evidence_before_parsing(self):
        from unbake.decomp import gbi_recover
        from unbake.fold import callee_contracts, imports, pool_literals, source_views
        from unbake.typemap import declaration_evidence

        def parsed(project, policy, text, versions, function, headers):
            canonical(self, text)
            canonical(self, headers.texts[self.header])
            raise ValueError("canonical fold reached normal parser")

        prefix = "extern s32 func_8011F810;\n"
        text = self.source.read_text().replace("extern s32 func_8011F810;", "")
        with (
            patch.object(callee_contracts, "reconcile", side_effect=lambda project, headers, *args: (headers, [])),
            patch.object(gbi_recover, "import_aliases", side_effect=lambda project, text, *args, **kwargs: text),
            patch.object(
                declaration_evidence,
                "inject",
                side_effect=lambda project, headers, text, *args: (prefix + text, len(prefix)),
            ) as injected,
            patch.object(imports, "resolve", side_effect=lambda project, headers, text, *args: text),
            patch.object(pool_literals, "lower", side_effect=lambda project, function, text, versions: text),
            patch.object(source_views, "parsers", side_effect=parsed) as parses,
            self.assertRaisesRegex(ValueError, "canonical fold reached normal parser"),
        ):
            fold_declarations.fold_source(
                self.project, self.host, Headers.read(self.project), "alpha", text, self.versions
            )
        self.assertEqual(injected.call_count, 1)
        self.assertEqual(parses.call_count, 1)

    def test_consumer_edits_reuse_layout_changes_and_scan_each_version_once(self):
        contracts = namespace.FunctionDeclarations(VALUE, {self.header: HEADER})
        beta = self.project.src / "beta.c"
        beta.write_text("int beta(void) { return 2; }\n")
        before = self.source.read_text()
        edit = split.Edit(self.source, before, before.replace("arg0->unk40 = 1", "arg0->unk40 = 2"), self.versions)
        with patch.object(split, "owners_by_alias", wraps=split.owners_by_alias) as owners:
            edits = namespace.consumer_edits(self.project, contracts, "gamma", (edit,))
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].before, before)
        self.assertIn("arg0->unk40 = 2", edits[0].after)
        canonical(self, edits[0].after)
        self.assertEqual(owners.call_count, len(self.versions))

    def test_m2c_scalar_receipts_are_canonical_before_its_single_compile_proof(self):
        from contextlib import ExitStack

        from unbake.decomp import m2c

        text = self.source.read_text()
        assembly = "glabel alpha\n addiu $v0, $zero, 1\n jr $ra\n nop\n"

        def proved(project, host, function, version, candidate):
            canonical(self, candidate.read_text())
            canonical(self, (project.work_include[0] / self.header.relative_to(self.project.include[0])).read_text())
            self.assertEqual(function, "alpha")
            self.assertEqual(version, "us")
            self.assertEqual(self.header.read_text(), HEADER)

        def passthrough(text, *args, **kwargs):
            return text

        with ExitStack() as stack:
            for name in ("normalize", "stack_locals", "header_types", "lower", "address_arithmetic"):
                stack.enter_context(patch.object(m2c, name, side_effect=passthrough))
            stack.enter_context(patch.object(m2c, "assembly_source", return_value=(assembly, 0x80001000)))
            stack.enter_context(
                patch.object(m2c, "canonical_entry", side_effect=lambda project, v, f, a, text, **kw: text)
            )
            stack.enter_context(
                patch.object(m2c, "canonical_aliases", side_effect=lambda project, v, text, *args: text)
            )
            stack.enter_context(
                patch.object(m2c, "private_constants", side_effect=lambda project, v, f, text, **kw: text)
            )
            stack.enter_context(patch.object(m2c, "jump_tables", side_effect=lambda project, v, f, text: text))
            stack.enter_context(patch.object(m2c.similar, "retrieve", return_value=[]))
            stack.enter_context(patch.object(m2c, "preprocess_context", return_value="typedef int s32;"))
            stack.enter_context(patch.object(m2c.draft_abi, "declarations", return_value=""))
            stack.enter_context(patch.object(m2c.measured_storage, "prepare", return_value=(text, None)))
            stack.enter_context(patch.object(m2c, "share", return_value=(text, None)))
            stack.enter_context(
                patch.object(m2c.gbi, "prepare", return_value=SimpleNamespace(source=text, headers=(), raw=()))
            )
            generated = stack.enter_context(patch.object(m2c, "run_tool", return_value=text))
            proved_native = stack.enter_context(patch.object(m2c, "prove", side_effect=proved))
            result = m2c.draft(
                self.project,
                self.host,
                "alpha",
                "us",
                self.project.work / "alpha/m2c",
                self.project.root / "extract",
                type_context="",
            )
        canonical(self, result)
        self.assertEqual(generated.call_count, 1)
        self.assertEqual(proved_native.call_count, 1)
