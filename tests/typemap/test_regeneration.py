"""Incremental publish contracts with injected parser and preprocessor boundaries."""

import copy
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.project import cache
from unbake.project.config import Held
from unbake.typemap import database, header_names, regeneration, split, storage


class RegenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.project, self.policy, _ = fixture(Path(temporary.name), case=self)
        self.policy.cache_root = self.project.root / ".cache"
        self.policy.m2c = Path("/fixture/m2c")
        self.root = self.project.include[0]
        self.value = {
            **storage.identity(self.project),
            "revision": 1,
            "functions": {name: {"state": "known", "prototype": f"int {name}(void);"} for name in ("f", "g")},
            "globals": {},
            "arrays": {},
            "structs": {},
            "typedefs": {},
            "dependencies": {"f": [], "g": []},
            "unknown": [],
            "conflicts": [],
        }

    def publish(self):
        with (
            patch("unbake.decomp.draft_context.preprocess_context", return_value="int preprocessed;") as cpp,
            patch("unbake.decomp.trial_compile.run_tool", return_value="") as parser,
        ):
            database.publish(self.project, copy.deepcopy(self.value), {}, policy=self.policy)
        return cpp, parser

    def headers(self):
        return {p: p.read_bytes() for p in self.root.rglob("*.h") if storage.generated(self.project, p)}

    def test_cold_miss_and_warm_hit_have_identical_bytes_without_render_parse_or_install(self):
        cpp, parser = self.publish()
        self.assertEqual((cpp.call_count, parser.call_count), (1, 1))
        before = self.headers()
        stamps = {p: p.stat().st_ino for p in before}
        with (
            patch.object(database, "_render", side_effect=AssertionError("rendered")),
            patch.object(storage, "write", wraps=storage.write) as writes,
        ):
            cpp, parser = self.publish()
        self.assertEqual((cpp.call_count, parser.call_count), (0, 0))
        self.assertEqual(before, self.headers())
        self.assertEqual(stamps, {p: p.stat().st_ino for p in before})
        self.assertFalse(any(storage.generated(self.project, call.args[0]) for call in writes.call_args_list))

    def test_function_change_reuses_layout_and_renders_only_changed_guarded_header(self):
        self.publish()
        sibling = self.root / "shared/decls/g.h"
        stamp = sibling.stat().st_ino
        self.value["functions"]["f"]["prototype"] = "unsigned int f(void);"
        with (
            patch.object(split.Layout, "__init__", side_effect=AssertionError("layout reparsed")),
            patch.object(split, "guarded", wraps=split.guarded) as render,
        ):
            cpp, parser = self.publish()
        self.assertEqual((cpp.call_count, parser.call_count), (1, 1))
        self.assertEqual([call.args[0].name for call in render.call_args_list], ["f.h"])
        self.assertEqual(stamp, sibling.stat().st_ino)
        self.assertIn(b"unsigned int f", (self.root / "shared/decls/f.h").read_bytes())

    def test_render_input_kinds_invalidate_and_restore_byte_identity(self):
        cases = [
            ("function", lambda: self.value["functions"]["f"].update(prototype="short f(void);")),
            (
                "global",
                lambda: self.value["globals"].update(item={"state": "known", "declaration": "extern int item;"}),
            ),
            ("array", lambda: self.value["arrays"].update(items={"state": "known", "partial": True, "type": "int"})),
            (
                "struct",
                lambda: self.value["structs"].update(
                    A={"state": "known", "generated": True, "type": "struct A", "declaration": "struct A { int x; };"}
                ),
            ),
            ("typedef", lambda: self.value["typedefs"].update(Word="int")),
            ("authored", lambda: (self.root / "extra.h").write_text("typedef int Extra;")),
            ("source", lambda: (self.project.src / "f.c").write_text("int f(void) { return 1; }")),
        ]
        for name, change in cases:
            with self.subTest(input=name):
                baseline = copy.deepcopy(self.value)
                self.publish()
                before = self.headers()
                change()
                with patch.object(database, "_render", wraps=database._render) as render:
                    self.publish()
                self.assertEqual(render.call_count, 0 if name == "function" else 1)
                self.value = baseline
                (self.root / "extra.h").unlink(missing_ok=True)
                (self.project.src / "f.c").unlink(missing_ok=True)
                self.publish()
                self.assertEqual(before, self.headers())

    def test_metadata_does_not_invalidate_render_or_validation(self):
        self.publish()
        for field in ("provenance", "versions", "users", "reasons"):
            with self.subTest(field=field):
                self.value["functions"]["f"][field] = ["new evidence"]
                with patch.object(database, "_render", side_effect=AssertionError("metadata rendered")):
                    cpp, parser = self.publish()
                self.assertEqual((cpp.call_count, parser.call_count), (0, 0))

    def test_deleting_a_declaration_removes_only_its_header(self):
        self.publish()
        sibling = self.root / "shared/decls/g.h"
        stamp = sibling.stat().st_ino
        del self.value["functions"]["f"]
        self.publish()
        self.assertFalse((self.root / "shared/decls/f.h").exists())
        self.assertEqual(stamp, sibling.stat().st_ino)

    def test_deleted_and_edited_generated_outputs_are_restored_from_cache(self):
        self.publish()
        expected = self.headers()
        (self.root / "shared/decls/f.h").unlink()
        (self.root / "shared/decls/g.h").write_text("broken")
        with patch.object(database, "_render", side_effect=AssertionError("rendered")):
            cpp, parser = self.publish()
        self.assertEqual((cpp.call_count, parser.call_count), (0, 0))
        self.assertEqual(expected, self.headers())

    def test_environment_kinds_invalidate_both_render_and_validation(self):
        cases = ["config", "policy", "macros", "compiler", "generator", "tool"]
        for name in cases:
            with self.subTest(input=name):
                self.publish()
                project, flags = self.project, self.policy.cppflags
                config = self.project.root / "config.toml"
                text = config.read_text()
                context = patch("unbake.typemap.regeneration.environment", wraps=regeneration.environment)
                if name == "config":
                    config.write_text(text + "\n")
                elif name == "policy":
                    self.policy.cppflags = (*flags, "-DCHANGED=1")
                elif name == "macros":
                    version = self.project.version("us")
                    self.project = replace(self.project, version_map={"us": replace(version, macros=("CHANGED",))})
                elif name == "compiler":
                    compilers = {
                        k: replace(c, cflags=(*c.cflags, "-DCHANGED")) for k, c in self.project.compilers.items()
                    }
                    self.project = replace(self.project, compilers=compilers)
                elif name == "generator":
                    context = patch("unbake.typemap.regeneration.environment", return_value=cache.key("new-generator"))
                else:
                    executable = self.project.root / "m2c"
                    executable.write_text("new tool bytes")
                    self.policy.m2c = executable
                with context, patch.object(database, "_render", wraps=database._render) as render:
                    cpp, parser = self.publish()
                self.assertEqual(render.call_count, 1)
                self.assertEqual((cpp.call_count, parser.call_count), (1, 1))
                self.project, self.policy.cppflags = project, flags
                self.policy.m2c = Path("/fixture/m2c")
                config.write_text(text)

    def test_wrapper_projection_is_stable_and_external_edit_invalidates(self):
        wrapper = self.root / "wrapper.h"
        wrapper.write_text('#include "shared/typemap.h"\nstruct A { int x; };\n')
        self.publish()
        before = self.headers()
        wrapper_bytes = wrapper.read_bytes()
        with patch.object(database, "_render", side_effect=AssertionError("wrapper rerendered")):
            self.publish()
        self.assertEqual(wrapper_bytes, wrapper.read_bytes())
        self.assertEqual(before, self.headers())
        wrapper.write_bytes(wrapper_bytes.replace(b"int x", b"short x"))
        with patch.object(database, "_render", wraps=database._render) as render:
            self.publish()
        self.assertEqual(render.call_count, 1)
        self.assertTrue(any(b"short x" in data for data in self.headers().values()))

    def test_validation_failure_preserves_previous_revision_and_outputs(self):
        self.publish()
        before = self.headers()
        revision = (self.project.build / "types/database.json").read_bytes()
        self.value["functions"]["f"]["prototype"] = "short f(void);"
        with (
            patch("unbake.decomp.draft_context.preprocess_context", return_value="changed"),
            patch("unbake.decomp.trial_compile.run_tool", side_effect=Held("solve", "invalid types")),
            self.assertRaisesRegex(Held, "types.header_parse"),
        ):
            database.publish(self.project, copy.deepcopy(self.value), {}, policy=self.policy)
        self.assertEqual(before, self.headers())
        self.assertEqual(revision, (self.project.build / "types/database.json").read_bytes())
        cpp, parser = self.publish()
        self.assertEqual((cpp.call_count, parser.call_count), (1, 1))

    def test_failed_install_rolls_back_changed_outputs_and_deletions(self):
        self.publish()
        before = self.headers()
        del self.value["functions"]["g"]
        self.value["functions"]["f"]["prototype"] = "short f(void);"
        original = storage.write

        def fail(path, data):
            if path.name == "summary.json":
                raise OSError("failed install")
            original(path, data)

        with (
            patch.object(database, "validate_headers"),
            patch.object(storage, "write", side_effect=fail),
            self.assertRaises(OSError),
        ):
            database.publish(self.project, copy.deepcopy(self.value), {}, policy=self.policy)
        self.assertEqual(before, self.headers())

    def test_persistent_artifacts_survive_a_fresh_process_cache(self):
        self.publish()
        with (
            patch.object(cache, "_parsed", {}),
            patch.object(cache, "_remembered", {}),
            patch.object(database, "_render", side_effect=AssertionError("persistent render missed")),
        ):
            cpp, parser = self.publish()
        self.assertEqual((cpp.call_count, parser.call_count), (0, 0))

    def test_serialization_observes_in_place_mutation_and_is_byte_identical(self):
        value = {"nodes": {"a": [1, {"b": 2}]}, "schema": 1, "unicode": "é"}
        path = self.project.build / "types/encoded.json"
        for change in (False, True):
            if change:
                value["nodes"]["a"][1]["b"] = 3
            staged = storage.stage_json(path, value)
            self.assertEqual(staged.read_bytes(), storage.encoded(value))
            storage.discard_json(staged)

    def test_serialized_cache_hit_skips_encoder_and_failed_compute_keeps_snapshot(self):
        value = {"a": [1]}
        with patch.object(cache, "_serialized", {}):
            expected = cache.serialized("fixture", value)
            with patch.object(cache.json, "dumps", side_effect=AssertionError("encoded twice")):
                self.assertEqual(cache.serialized("fixture", copy.deepcopy(value)), expected)
            value["a"].append(2)
            self.assertNotEqual(cache.serialized("fixture", value), expected)

    def test_type_layout_change_renders_only_changed_type_and_observes_alias_edges(self):
        session = regeneration.Session(self.project, self.policy)
        contents = {self.root / "a.h": "struct A { int x; };", self.root / "b.h": "struct B { int y; };"}
        first = session.layout(contents, contents, self.root, {})
        changed = {**contents, self.root / "a.h": "struct A { short x; };"}
        with patch.object(split, "guarded", wraps=split.guarded) as render:
            second = session.layout(changed, changed, self.root, {})
        self.assertEqual(len(render.call_args_list), 1)
        unchanged = first.homes[self.root / "b.h"]
        self.assertEqual(first.headers[unchanged], second.headers[unchanged])
        with patch.object(split.Layout, "__init__", wraps=None, side_effect=AssertionError("layout miss")):
            repeated = session.layout(changed, changed, self.root, {})
        self.assertEqual(second.headers, repeated.headers)
        with patch.object(split.Layout, "__init__", autospec=True, side_effect=split.Layout.__init__) as build:
            session.layout(changed, changed, self.root, {"Pointer": "struct A"})
        self.assertEqual(build.call_count, 1)

    def validate(self, outputs, *, abi=""):
        selections = []

        def expand(source, project, policy, version, function):
            staged = [str(p.relative_to(root)) for root in project.include for p in root.rglob("*.h")]
            selections.append(source.read_text() + "\n/* staged " + " ".join(staged) + " */")
            return source.read_text()

        with (
            patch("unbake.decomp.draft_context.preprocess_context", side_effect=expand),
            patch("unbake.decomp.trial_compile.run_tool", return_value="") as parser,
        ):
            database.validate_headers(self.project, outputs, self.policy, abi_context=abi)
        return selections, parser

    def validation_fixture(self):
        shared = self.root / "shared"
        return {
            shared / "types/a.h": b"typedef int A;\n",
            shared / "types/b.h": b"typedef int B;\n",
            shared / "decls/f.h": b'#include "shared/types/a.h"\nextern A f(void);\n',
            shared / "decls/g.h": b'#include "shared/types/b.h"\nextern B g(void);\n',
        }

    def test_validation_transitive_change_selects_dependants_and_excludes_siblings(self):
        outputs = self.validation_fixture()
        self.validate(outputs)
        outputs[self.root / "shared/types/a.h"] = b"typedef short A;\n"
        selections, parser = self.validate(outputs)
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(len(selections), 1)
        self.assertIn("shared/decls/f.h", selections[0])
        self.assertNotIn("shared/decls/g.h", selections[0])
        self.assertNotIn("shared/types/b.h", selections[0])

    def test_validation_authored_dependency_add_edit_delete_and_same_timestamp(self):
        outputs = self.validation_fixture()
        a = self.root / "shared/types/a.h"
        outputs[a] = b'#include "extra.h"\ntypedef int A;\n'
        extra = self.root / "extra.h"
        self.validate(outputs)
        for content in (b"typedef int Extra;", b"typedef short Extra;", None):
            with self.subTest(content=content):
                if content is None:
                    extra.unlink()
                else:
                    extra.write_bytes(content)
                    os.utime(extra, ns=(1, 1))
                selections, parser = self.validate(outputs)
                self.assertEqual(parser.call_count, 0 if content is None else 1)
                if content is not None:
                    self.assertIn("shared/decls/f.h", selections[0])
                    self.assertNotIn("shared/decls/g.h", selections[0])

    def test_abi_change_validates_only_required_types_and_same_named_declaration(self):
        outputs = self.validation_fixture()
        self.validate(outputs, abi="A f(void);")
        selections, parser = self.validate(outputs, abi="A f(int value);")
        self.assertEqual(parser.call_count, 1)
        self.assertIn("shared/types/a.h", selections[0])
        self.assertIn("shared/decls/f.h", selections[0])
        self.assertNotIn("shared/decls/g.h", selections[0])
        selections, parser = self.validate(outputs, abi="A f(int value);")
        self.assertEqual((len(selections), parser.call_count), (0, 0))

    def test_relative_include_normalization_and_include_cycle(self):
        outputs = self.validation_fixture()
        a = self.root / "shared/types/a.h"
        b = self.root / "shared/types/b.h"
        outputs[a] = b'#include "../types/b.h"\ntypedef int A;'
        outputs[b] = b'#include "a.h"\ntypedef int B;'
        _, closures, _ = regeneration.validation_inputs(self.project, outputs, "")
        self.assertEqual(closures[a], {a, b})
        self.assertEqual(closures[self.root / "shared/decls/f.h"], {a, b, self.root / "shared/decls/f.h"})

    def test_source_ownership_cached_per_body_when_a_sibling_changes(self):
        for name in ("f", "g"):
            (self.project.src / (name + ".c")).write_text(
                f'#include "shared/decls/{name}.h"\ntypedef int Local_{name};'
            )
        self.publish()
        (self.project.src / "f.c").write_text('#include "shared/decls/f.h"\ntypedef int NewLocal;')
        from unbake.typemap import header_names

        original = header_names._Declarations.parse
        inputs = []

        def parse(parser):
            inputs.append(parser.source)
            return original(parser)

        with patch.object(header_names._Declarations, "parse", parse):
            self.publish()
        self.assertTrue(any("NewLocal" in text for text in inputs))
        self.assertFalse(any("Local_g" in text for text in inputs))

    def test_deleted_authored_header_and_source_invalidate_render(self):
        for path, text in (
            (self.root / "extra.h", "typedef int Extra;"),
            (self.project.src / "extra.c", "int extra(void) { return 0; }"),
        ):
            with self.subTest(path=path):
                path.write_text(text)
                self.publish()
                path.unlink()
                with patch.object(database, "_render", wraps=database._render) as render:
                    self.publish()
                # Deletion returns to a previously observed content key; reuse
                # that prior artifact rather than rerendering the same inputs.
                self.assertEqual(render.call_count, 1 if path.suffix == ".h" else 0)

    def test_tool_content_changes_at_same_path_invalidate_environment(self):
        tool = self.project.root / "m2c"
        self.policy.m2c = tool
        tool.write_bytes(b"first")
        self.publish()
        tool.write_bytes(b"other")
        os.utime(tool, ns=(1, 1))
        with patch.object(database, "_render", wraps=database._render) as render:
            cpp, parser = self.publish()
        self.assertEqual((render.call_count, cpp.call_count, parser.call_count), (1, 1, 1))

    def test_serialization_failure_does_not_cache_partial_value(self):
        with patch.object(cache, "_serialized", {}):
            expected = cache.serialized("fixture", {"a": 1})
            with self.assertRaises(TypeError):
                cache.serialized("fixture", {"a": object()})
            self.assertEqual(cache.serialized("fixture", {"a": 1}), expected)

    def test_include_only_index_observes_authored_inputs_without_revalidating_unchanged_indexes(self):
        external = self.root / "external.h"
        external.write_text("typedef int External;")
        index = self.root / "shared/types/index.h"
        outputs = {index: b'#include "external.h"\n'}
        self.validate(outputs)
        external.write_text("typedef short External;")
        selections, parser = self.validate(outputs)
        self.assertEqual(parser.call_count, 1)
        self.assertIn("external.h", selections[0])

    def test_artifacts_retain_only_bytes_and_isolate_mutated_results(self):
        store = cache.Cache(self.policy.cache_root)
        identity = cache.key("artifact")
        first = regeneration.artifact(store, "fixture", identity, lambda: {"items": [{"x": 1}]})
        first["items"][0]["x"] = 9
        for fresh_process in (False, True):
            with self.subTest(fresh_process=fresh_process):
                context = (
                    patch.object(cache, "_remembered", {}) if fresh_process else patch.object(cache, "_parsed", {})
                )
                with context:
                    actual = regeneration.artifact(store, "fixture", identity, lambda: self.fail("artifact recomputed"))
                self.assertEqual(actual, {"items": [{"x": 1}]})
        with patch.object(store, "get", side_effect=AssertionError("artifact rehashed")):
            self.assertEqual(regeneration.artifact(store, "fixture", identity, lambda: None), {"items": [{"x": 1}]})

    def test_certificates_add_atomic_batches_and_merge_independent_publishers(self):
        store = cache.Cache(self.policy.cache_root)
        environment = cache.key("certificates")
        first, other = (regeneration.Certificates(store, environment) for _ in range(2))
        self.assertFalse(first.contains("missing"))
        self.assertFalse(other.contains("missing"))
        first.add({"us:one", "us:two"})
        other.add({"eu:three"})
        merged = regeneration.Certificates(store, environment)
        for name in ("us:one", "us:two", "eu:three"):
            with self.subTest(name=name):
                self.assertTrue(merged.contains(name))
        self.assertFalse(merged.contains("us:three"))
        self.assertFalse(regeneration.Certificates(store, cache.key("different-env")).contains("us:one"))
        self.assertEqual(len(list(first.directory.iterdir())), 2)
        # add() may be the first operation on a fresh store.
        fresh = regeneration.Certificates(store, environment)
        fresh.add({"us:four"})
        self.assertTrue(fresh.contains("us:one"))
        for row in first.directory.iterdir():
            self.assertTrue(row.name.endswith(".json"))

    def test_failed_certificate_write_keeps_existing_batches_and_memory(self):
        store = regeneration.Certificates(cache.Cache(self.policy.cache_root), cache.key("certificate-failure"))
        store.add({"previous"})
        with patch.object(storage.os, "replace", side_effect=OSError("failed replace")), self.assertRaises(OSError):
            store.add({"pending"})
        self.assertTrue(store.contains("previous"))
        self.assertFalse(store.contains("pending"))
        self.assertEqual(len(list(store.directory.iterdir())), 1)

    def test_validation_batches_all_leaves_and_reuses_certificates_in_fresh_session(self):
        outputs = self.validation_fixture()
        selections, parser = self.validate(outputs)
        self.assertEqual((len(selections), parser.call_count), (1, 1))
        batches = list((self.policy.cache_root / "typemap-certificates").rglob("*.json"))
        self.assertEqual(len(batches), len(self.project.versions))
        self.assertFalse((self.policy.cache_root / "typemap-validated").exists())
        # A new unrelated declaration changes the whole bundle, but only that
        # declaration should be staged after reloading persisted certificates.
        outputs[self.root / "shared/decls/new.h"] = b"extern int new(void);"
        with patch.object(cache, "_remembered", {}):
            selections, parser = self.validate(outputs)
        self.assertEqual(parser.call_count, 1)
        self.assertIn("shared/decls/new.h", selections[0])
        for name in ("shared/decls/f.h", "shared/decls/g.h"):
            self.assertNotIn(name, selections[0])

    def test_rewrite_skips_unaffected_tokens_and_observes_in_place_context_changes(self):
        session = regeneration.Session(self.project, self.policy)
        replacements, blocked = {"Alias": "int"}, {"Local"}
        with patch.object(header_names, "rewrite", wraps=header_names.rewrite) as rewrite:
            self.assertEqual(session.rewrite("int f(int arg);", replacements, blocked), "int f(int arg);")
            self.assertEqual(rewrite.call_count, 0)
            for target in ("int", "short"):
                with self.subTest(target=target):
                    replacements["Alias"] = target
                    self.assertEqual(session.rewrite("Alias f(void);", replacements, blocked), target + " f(void);")
            self.assertEqual(session.rewrite("typedef int Extra;", replacements, blocked), "typedef int Extra;")
            blocked.add("Extra")
            self.assertEqual(session.rewrite("typedef int Extra;", replacements, blocked), "")
            self.assertEqual(session.rewrite("typedef int M2C_UNK32;", {}, set()), "")

    def test_generated_path_prefixes_respect_component_boundaries(self):
        cases = [
            ("shared/typemap.h", True),
            ("shared/prototypes.h", True),
            ("shared/types/a.h", True),
            ("shared/types/nested/a.h", True),
            ("shared/decls/a.h", True),
            ("shared/consumers/a.h", True),
            ("shared/types", True),
            ("shared/types_extra/a.h", False),
            ("shared/decls_extra/a.h", False),
            ("shared/consumers_extra/a.h", False),
            ("shared/typemap.h.old", False),
            ("other/shared/types/a.h", False),
        ]
        for name, expected in cases:
            with (
                self.subTest(name=name),
                patch.object(Path, "is_relative_to", side_effect=AssertionError("ancestor scan")),
            ):
                self.assertEqual(storage.generated(self.project, self.root / name), expected)
        self.assertFalse(storage.generated(replace(self.project, include=()), self.root / "shared/types/a.h"))
        sibling = self.root.with_name(self.root.name + "_extra") / "shared/types/a.h"
        self.assertFalse(storage.generated(self.project, sibling))

    def test_project_relative_strings_preserve_relative_to_results(self):
        for relative in (".", "include/shared/types/a.h", "src/f.c"):
            with self.subTest(relative=relative):
                path = self.project.root / relative
                self.assertEqual(storage.relative(self.project, path), str(path.relative_to(self.project.root)))
        with self.assertRaises(ValueError):
            storage.relative(self.project, self.project.root.with_name("outside") / "f.c")

    def test_source_ownership_transitive_cycles_virtual_leaves_and_prefix_boundaries(self):
        bridge = self.root / "bridge.h"
        other = self.root / "other.h"
        bridge.write_text('#include "other.h"\n')
        other.write_text('#include "bridge.h"\n#include "shared/types/missing.h"\n')
        cases = [
            ("transitive", "bridge.h", {"Owned"}),
            ("generated", "shared/decls/missing.h", {"Owned"}),
            ("prototype", "shared/prototypes.h", {"Owned"}),
            ("sibling", "shared/types_extra/missing.h", set()),
            ("unrelated", "missing.h", set()),
        ]
        for name, include, expected in cases:
            with self.subTest(name=name):
                source = self.project.src / "f.c"
                source.write_text(f'#include "{include}"\ntypedef int Owned;\n')
                texts = {p: p.read_text() for p in (source, bridge, other)}
                consumers = {}
                with patch.object(Path, "is_relative_to", side_effect=AssertionError("ancestor scan")):
                    names = header_names.source_names(
                        self.project, self.root / "shared/typemap.h", self.policy, consumers=consumers, texts=texts
                    )
                self.assertEqual(names, expected)
                self.assertEqual(consumers.get(source, set()), expected)

    def test_serialized_snapshots_share_no_mutable_objects_with_the_caller(self):
        for value in ({"a": [{"b": 1}]}, [{"a": [1]}]):
            with self.subTest(value=value), patch.object(cache, "_serialized", {}):
                before = cache.serialized("independent", value)
                snapshot = cache._serialized["independent"][0]
                self.assertIsNot(snapshot, value)
                if isinstance(value, dict):
                    value["a"][0]["b"] = 2
                else:
                    value[0]["a"].append(2)
                self.assertEqual(cache.json.loads(before), snapshot)
                self.assertNotEqual(cache.serialized("independent", value), before)

    def test_prototype_delta_matches_full_render_across_processes_and_type_dependencies(self):
        self.value["typedefs"] = {"A": "int", "B": "short"}
        source = self.project.src / "f.c"
        source.write_text('#include "shared/decls/f.h"\ntypedef int Local;\n')
        self.publish()
        for prototype in ("A f(B value);", "static B f(A value);", "extern int f(void);"):
            with self.subTest(prototype=prototype):
                self.value["functions"]["f"]["prototype"] = prototype
                with (
                    patch.object(cache, "_remembered", {}),
                    patch.object(database, "_render", side_effect=AssertionError("full render on prototype edit")),
                ):
                    self.publish()
                session = regeneration.Session(self.project, self.policy)
                reference = database._render(self.project, copy.deepcopy(self.value), self.policy, session)
                self.assertEqual(
                    self.headers(), {p: data for p, data in reference.items() if storage.generated(self.project, p)}
                )

    def test_prototype_delta_preserves_authored_tag_ownership(self):
        (self.root / "local.h").write_text("struct LocalTag { int value; };")
        (self.project.src / "f.c").write_text(
            '#include "local.h"\n#include "shared/decls/f.h"\nstruct LocalTag *f(void);'
        )
        self.value["functions"]["f"]["prototype"] = "struct LocalTag *f(void);"
        self.publish()
        for prototype in ("struct LocalTag *f(int value);", "void f(struct LocalTag *value);"):
            with self.subTest(prototype=prototype):
                self.value["functions"]["f"]["prototype"] = prototype
                with patch.object(database, "_render", side_effect=AssertionError("full render on prototype edit")):
                    self.publish()
                header = self.root / "shared/decls/f.h"
                self.assertNotIn(b"#include", header.read_bytes())
                session = regeneration.Session(self.project, self.policy)
                reference = database._render(self.project, copy.deepcopy(self.value), self.policy, session)
                self.assertEqual(
                    self.headers(), {p: data for p, data in reference.items() if storage.generated(self.project, p)}
                )

    def test_prototype_delta_falls_back_when_state_or_render_artifact_is_missing(self):
        for missing in ("state", "artifact"):
            with self.subTest(missing=missing):
                self.publish()
                session = regeneration.Session(self.project, self.policy)
                state = session.cache.path(
                    "typemap-render-state", cache.key(session.inputs, str((self.root / "shared/typemap.h").is_file()))
                )
                record = cache.json.loads(state.read_bytes())
                if missing == "state":
                    state.unlink()
                else:
                    session.cache.path("typemap-render", record["content_key"]).unlink()
                self.value["functions"]["f"]["prototype"] = "short f(void);" if missing == "state" else "long f(void);"
                with (
                    patch.object(cache, "_remembered", {}),
                    patch.object(database, "_render", wraps=database._render) as render,
                ):
                    self.publish()
                self.assertEqual(render.call_count, 1)

    def test_default_abi_is_batched_with_each_variant_and_failures_remain_atomic(self):
        self.value["functions"]["f"]["abi_declaration"] = {
            "prototype": "int f(void);",
            "variants": {"a0": {"prototype": "int first(void);"}, "a1": {"prototype": "int second(void);"}},
        }
        contexts = []
        original = database.validate_headers

        def validate(*args, **kwargs):
            contexts.append(kwargs["abi_context"])
            return original(*args, **kwargs)

        with patch.object(database, "validate_headers", side_effect=validate):
            cpp, parser = self.publish()
        self.assertEqual((len(contexts), cpp.call_count, parser.call_count), (2, 2, 2))
        for text, name in zip(contexts, ("first", "second"), strict=True):
            self.assertIn("int f(void);", text)
            self.assertIn(name, text)
        before = self.headers()
        self.value["functions"]["f"]["prototype"] = "short f(void);"
        with (
            patch("unbake.decomp.draft_context.preprocess_context", return_value="changed"),
            patch("unbake.decomp.trial_compile.run_tool", side_effect=Held("solve", "bad default ABI")),
            self.assertRaisesRegex(Held, "types.header_parse"),
        ):
            database.publish(self.project, copy.deepcopy(self.value), {}, policy=self.policy)
        self.assertEqual(before, self.headers())

    def test_unchanged_database_skips_staging_and_rehashing_without_missing_file_edits(self):
        path = self.project.build / "types/cache.json"
        value = {"schema": 1, "nodes": {"a": [1]}}
        staged, digest = storage.database_json(path, value)
        self.assertIsNotNone(staged)
        storage.install(path, staged)
        with patch.object(storage, "_stage_json", side_effect=AssertionError("staged unchanged JSON")):
            current, actual = storage.database_json(path, copy.deepcopy(value))
        self.assertEqual((current, actual), (None, digest))
        original = path.stat()
        for content in (b"broken", None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                staged, actual = storage.database_json(path, value)
                self.assertIsNotNone(staged)
                self.assertEqual(staged.read_bytes(), storage.encoded(value))
                storage.install(path, staged)
                self.assertEqual(actual, digest)
        value["nodes"]["a"].append(2)
        staged, actual = storage.database_json(path, value)
        self.assertNotEqual(actual, digest)
        storage.discard_json(staged)
        self.assertNotIn(staged, storage._json_stages)

    def test_database_snapshot_is_invalidated_by_failed_publication_rollback(self):
        self.publish()
        before = (self.project.build / "types/database.json").read_bytes()
        self.value["functions"]["f"]["prototype"] = "short f(void);"
        original = storage.write

        def fail(path, data):
            if path.name == "summary.json":
                raise OSError("summary install failed")
            return original(path, data)

        with patch.object(storage, "write", side_effect=fail), self.assertRaises(OSError):
            self.publish()
        self.assertEqual((self.project.build / "types/database.json").read_bytes(), before)
        self.value["functions"]["f"]["prototype"] = "int f(void);"
        self.publish()
        self.assertEqual((self.project.build / "types/database.json").read_bytes(), before)

    def test_file_digest_reuses_verified_stamp_and_observes_same_mtime_and_atomic_replacement(self):
        path = self.project.root / "digest.bin"
        path.write_bytes(b"first")
        expected = storage.file_digest(path)
        with patch.object(Path, "open", side_effect=AssertionError("rehashed unchanged file")):
            self.assertEqual(storage.file_digest(path), expected)
        stamp = path.stat()
        path.write_bytes(b"other")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertNotEqual(storage.file_digest(path), expected)
        replacement = path.with_suffix(".new")
        replacement.write_bytes(b"first")
        replacement.replace(path)
        self.assertEqual(storage.file_digest(path), expected)

    def test_database_staging_failures_and_symlinks_do_not_publish_or_poison_snapshots(self):
        path = self.project.build / "types/new.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        target = path.with_name("target.json")
        target.write_bytes(b"existing")
        path.symlink_to(target)
        for stage in (storage.stage_json, storage.database_json):
            with self.subTest(stage=stage.__name__), self.assertRaisesRegex(Held, "symlink"):
                stage(path, {"a": 1})
        self.assertEqual(target.read_bytes(), b"existing")
        path.unlink()
        with patch.object(storage.hashlib, "sha256", side_effect=OSError("digest failed")), self.assertRaises(OSError):
            storage.database_json(path, {"a": 1})
        self.assertFalse(path.exists())
        # The failing digest must not leave an open descriptor or temporary file.
        self.assertEqual(list(path.parent.iterdir()), [target])

    def test_source_without_owned_type_tokens_skips_parser_and_conditional_preprocessor(self):
        source = self.project.src / "f.c"
        source.write_text('#include "shared/decls/f.h"\n#if UNKNOWN_HEADER_MACRO\nint f(void) { return 1; }\n#endif\n')
        with (
            patch.object(header_names._Declarations, "parse", side_effect=AssertionError("empty name parse")),
            patch(
                "unbake.match.source_views._preprocessed_lines", side_effect=AssertionError("empty name preprocessing")
            ),
        ):
            consumers = {}
            names = header_names.source_names(
                self.project, self.root / "shared/typemap.h", self.policy, consumers=consumers
            )
        self.assertEqual((names, consumers[source]), (set(), set()))

    def test_include_strings_preserve_relative_paths_without_ancestor_scans(self):
        layout = split.Layout.__new__(split.Layout)
        layout.root = self.root
        for name in ("shared/types/a.h", "shared/types/deep/b.h", "."):
            with (
                self.subTest(name=name),
                patch.object(Path, "relative_to", side_effect=AssertionError("relative scan")),
            ):
                self.assertEqual(layout.include(self.root / name), f'#include "{name}"')
        with self.assertRaises(ValueError):
            layout.include(self.root.with_name("outside") / "types/a.h")
