"""Real closed headers converge on one owner before the public fold parses them."""

from collections import Counter
import hashlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import config
from unbake.config import Held
from unbake.fold import declarations
from unbake.layout.header_context import Headers
from unbake.layout import split
from unbake.layout.split import Edit
from unbake.project.headers import include_headers

FIXTURES = Path(__file__).parent / "fixtures/shared_provider"


def _linked(project, host, obj, version, row, source, *, capture_info=None):
    """Score-mode link mock that records the native digest chain the real link captures."""
    if capture_info is not None:
        words = split.words(project, row)
        digest = lambda label: hashlib.sha256(label.encode()).hexdigest()  # noqa: E731
        capture_info.update(
            preprocessed_sha256=digest("preprocessed"),
            object_sha256=digest("object"),
            placed_object_sha256=digest("placed"),
            linked_sha256=hashlib.sha256(words).hexdigest(),
            compiler_pins=digest("pins"),
            compile_argv=["cc"],
            placement={"address": row.address, "rom_start": row.start, "rom_end": row.end},
            placement_refusals=[],
        )
    return split.words(project, row), []


class ProviderReuseTests(ProjectCase):
    def contents(self):
        private = self.project.work / "alpha/include/shared"
        private.mkdir(parents=True)
        shared = self.project.include[-1] / "shared"
        shared.mkdir(parents=True)
        for name in ("func_80204C34_de", "func_802052C4_de"):
            (private / f"{name}_closed.h").write_bytes((FIXTURES / f"{name}_closed.h").read_bytes())
        (shared / "func_80206724_de_closed.h").write_bytes((FIXTURES / "func_80206724_de_closed.h").read_bytes())
        (self.project.include[-1] / "types.h").write_text("typedef unsigned int u32;\n")
        view = config.draft_view(self.project, "alpha")
        return view, {path: path.read_text() for path, _ in include_headers(view)}

    def test_public_fold_parses_one_owner_and_stages_each_consumer_once(self):
        view, contents = self.contents()
        before = dict(contents)
        calls = []

        def folded(project, host, headers, function, text, versions, **kwargs):
            calls.append(headers)
            self.assertEqual(len(headers.records), 2)
            self.assertEqual(headers.homes["struct Shared_CallbackHook"].parent, self.project.include[-1] / "shared")
            return declarations.Folded(function, text, [], {})

        with (
            patch.object(Headers, "contents", return_value=contents, create=True) as read,
            patch("unbake.layout.structs_fold._prove_includers") as proof,
            patch.object(declarations, "fold_source", side_effect=folded),
        ):
            edits = declarations.folded_edits(view, self.host, "alpha", "int alpha(void) { return 1; }", ("us", "eu"))
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(edits), 3)  # Two closed-header consumers plus the source.
        self.assertEqual(len({edit.path for edit in edits}), 3)
        self.assertEqual(contents, before)
        for edit in edits[:2]:
            self.assertEqual(edit.before, before[edit.path])
            self.assertIn('#include "shared/func_80206724_de_closed.h"', edit.after)
            self.assertNotIn("struct Shared_CallbackHook {", edit.after)
            self.assertEqual(edit.path.read_text(), edit.before)  # Plan writes nothing.
        self.assertIn("typedef void (*FuncPtr)(void);", edits[1].after)

    def test_real_payload_work_is_one_catalogue_per_header_and_no_reads_or_writes(self):
        from unbake.fold import provider_reuse

        view, contents = self.contents()
        with (
            patch.object(provider_reuse, "_catalog", wraps=provider_reuse._catalog) as catalog,
            patch.object(Path, "read_text", side_effect=AssertionError("planner reread a payload")),
            patch.object(Path, "write_text", side_effect=AssertionError("planner mutated a provider")),
        ):
            edits = provider_reuse.plan(view, contents, ("us",))
        self.assertEqual(catalog.call_count, 4)
        self.assertEqual(len(edits), 2)
        self.assertEqual(sum(edit.after.count("struct Shared_CallbackHook {") for edit in edits), 0)

    def test_unchanged_transitive_alias_needs_declaration_proof_and_retains_unproved_negatives(self):
        from unbake.fold import provider_reuse

        view, contents = self.contents()
        types = self.project.include[0] / "types.h"
        edits = provider_reuse.plan(view, contents, ("us",))
        self.assertEqual(len(edits), 2)
        self.assertNotIn(types, {edit.path for edit in edits})
        before = dict(contents)
        for conditional in (
            "#if VERSION\ntypedef unsigned int u32;\n#endif\n",
            "#ifndef TYPES_H\n#define TYPES_H\n#else\ntypedef unsigned int u32;\n#endif\n",
        ):
            with self.subTest(conditional=conditional), self.assertRaises(Held):
                provider_reuse.plan(view, {**contents, types: conditional}, ("us",))
        self.assertEqual(contents, before)

    def test_layout_and_callback_conflicts_name_both_locations_without_edits(self):
        from unbake.fold import provider_reuse

        view, contents = self.contents()
        first = next(path for path in contents if "func_80204C34" in path.name)
        owner = next(path for path in contents if "func_80206724" in path.name)
        for old, new in (
            ("unknown00[2]", "unknown00[3]"),
            ("(*Shared_VoidCallback)(void)", "(*Shared_VoidCallback)(int)"),
        ):
            with self.subTest(old=old), self.assertRaises(Held) as caught:
                provider_reuse.plan(view, {**contents, first: contents[first].replace(old, new)}, ("us",))
            self.assertEqual(caught.exception.key, "headers.declaration.duplicate-shared-provider")
            self.assertIn(first.relative_to(self.project.root).as_posix() + ":", caught.exception.reason)
            self.assertIn(owner.relative_to(self.project.root).as_posix() + ":", caught.exception.reason)

    def test_multiple_installed_equal_providers_fold_to_one_shared_home(self):
        from unbake.fold import provider_reuse

        _, contents = self.contents()
        public = {self.project.include[-1] / path.name: text for path, text in contents.items()}
        edits = provider_reuse.plan(self.project, public, ("us",))
        self.assertEqual(len(edits), 2)
        staged = {**public, **{edit.path: edit.after for edit in edits}}
        headers = Headers(staged, root=self.project.root)
        self.assertEqual(len(headers.records), 2)
        self.assertEqual(sum(text.count("struct Shared_CallbackHook {") for text in staged.values()), 1)

    def test_conditional_provider_cannot_be_silently_reused(self):
        from unbake.fold import provider_reuse

        view, contents = self.contents()
        first = next(path for path in contents if "func_80204C34" in path.name)
        with self.assertRaisesRegex(Held, "conditional or macro context"):
            provider_reuse.plan(
                view,
                {**contents, first: contents[first].replace("u32 unknown00", "#if VERSION\n u32 unknown00", 1)},
                ("us",),
            )

    def test_unrelated_conditional_macro_does_not_block_unconditional_provider_reuse(self):
        from unbake.fold import provider_reuse

        copy = self.project.include[-1] / "copy.h"
        owner = self.project.include[-1] / "owner.h"
        declaration = "struct Box { int value; };"
        unrelated = "#if VERSION\n#define LABEL(x) (x)\n#else\n#define LABEL(x) 0\n#endif\n"
        texts = {
            copy: "#ifndef COPY_H\n#define COPY_H\n" + unrelated + declaration + "\n#endif\n",
            owner: "#ifndef OWNER_H\n#define OWNER_H\n" + declaration + "\n#endif\n",
        }
        edits = provider_reuse.plan(self.project, texts, ("us",), changed=frozenset({copy}))
        self.assertEqual([edit.path for edit in edits], [copy])
        self.assertIn(unrelated, edits[0].after)
        self.assertNotIn("struct Box {", edits[0].after)
        for body in (
            "#if VERSION\n" + declaration + "\n#endif\n",
            "#define SIZE 1\nstruct Box { int value[SIZE]; };",
            "#if VERSION\ntypedef int Scalar;\n#endif\nstruct Box { Scalar value; };",
        ):
            with self.subTest(body=body), self.assertRaises(Held):
                provider_reuse.plan(
                    self.project,
                    {
                        copy: "#ifndef COPY_H\n#define COPY_H\n" + body + "\n#endif\n",
                        owner: texts[owner],
                    },
                    ("us",),
                    changed=frozenset({copy}),
                )

    def test_complete_typedef_body_removal_does_not_leave_duplicate_alias(self):
        from unbake.fold import provider_reuse

        private = self.project.work / "alpha/include/copy.h"
        owner = self.project.include[-1] / "owner.h"
        texts = {
            private: "#ifndef COPY_H\n#define COPY_H\ntypedef struct Box { int value; } Box;\n#endif\n",
            owner: "#ifndef OWNER_H\n#define OWNER_H\ntypedef struct Box { int value; } Box;\n#endif\n",
        }
        view = config.draft_view(self.project, "alpha")
        edits = provider_reuse.plan(view, texts, ("us",))
        self.assertEqual(len(edits), 1)
        self.assertNotIn("typedef", edits[0].after)
        self.assertEqual(len(Headers({**texts, private: edits[0].after}, root=self.project.root).records), 1)

    def test_final_fold_keeps_retained_consumer_import_and_proves_once(self):
        fixture = FIXTURES / "retained_consumer"
        root = self.project.include[-1]
        owner = root / "common/types_0847ae1836e8.h"
        target = root / "span_1000/code_800F45C8.h"
        owner_text = (fixture / owner.name).read_text()
        target_text = (fixture / target.name).read_text()
        owner.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        owner.write_text(owner_text)
        target.write_text(target_text)
        scalar = "typedef unsigned char u8; typedef unsigned int u32; typedef float f32;"
        contents = {owner: owner_text, target: target_text, root / "types.h": scalar}
        # Real generated definitions copied into the proposed consumer provider,
        # which still includes the prior common owner through its existing import.
        definitions = owner_text[owner_text.index("struct QueryResult;") : owner_text.rindex("#endif")]
        staged = target_text.replace("#endif", definitions + "\n#endif")
        native_calls = []

        def prove(project, edits, host, republished):
            effective = {**contents, **{edit.path: edit.after for edit in edits}}
            headers = Headers(effective, root=self.project.root)
            self.assertEqual(len(headers.records), 5)
            self.assertEqual(headers.homes["struct QueryBox"], owner)
            self.assertIn('#include "common/types_0847ae1836e8.h"', effective[target])
            self.assertNotIn("struct QueryBox {", effective[target])
            native_calls.append((len(edits), republished))

        with (
            patch.object(Headers, "contents", return_value=contents, create=True) as reads,
            patch.object(
                declarations,
                "fold_source",
                return_value=declarations.Folded(
                    "alpha", "void alpha(void) {}", [Edit(target, target_text, staged, ("us",))], {}
                ),
            ),
            patch("unbake.layout.structs_fold._prove_includers", side_effect=prove) as proof,
        ):
            edits = declarations.folded_edits(self.project, self.host, "alpha", "void alpha(void) {}", ("us",))
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(native_calls, [(1, self.project.src / "alpha.c")])
        self.assertEqual(len(edits), 2)
        self.assertEqual(edits[0].before, target_text)
        self.assertEqual(target.read_text(), target_text)
        # The real retained consumer keeps exactly its authored provider import.
        consumer = (fixture / "consumer.c").read_text()
        self.assertEqual(consumer.count('#include "span_1000/code_800F45C8.h"'), 1)

    def typedef_contents(self):
        fixture = FIXTURES / "typedef_only"
        root = self.project.include[-1]
        private = self.project.work / "alpha/include/shared/func_80245618_de_closed.h"
        private.parent.mkdir(parents=True)
        private.write_bytes((fixture / private.name).read_bytes())
        owner = root / "common/types_1dc8418c21db.h"
        owner.parent.mkdir(parents=True)
        owner.write_bytes((fixture / owner.name).read_bytes())
        wrapper = root / "shared/func_804235A8_eu_layout.h"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#include "common/types_1dc8418c21db.h"\n')
        (root / "common/types_8a8189af7b05.h").write_text("")
        (root / "types.h").write_text("typedef int s32;\n")
        return config.draft_view(self.project, "alpha"), private, owner

    def test_typedef_only_real_closed_header_uses_prior_owner_and_keeps_other_alias(self):
        from unbake.fold import provider_reuse

        view, private, owner = self.typedef_contents()
        contents = {path: path.read_text() for path, _ in include_headers(view)}
        with patch.object(provider_reuse, "_catalog", wraps=provider_reuse._catalog) as catalog:
            edits = provider_reuse.plan(view, contents, ("us", "eu"))
        self.assertEqual(catalog.call_count, 5)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].path, private)
        self.assertNotIn("(*VoidCallback)", edits[0].after)
        self.assertIn("(*ContextCallback)(void *)", edits[0].after)
        self.assertIn(owner.relative_to(self.project.include[-1]).as_posix(), edits[0].after)

    def test_public_comparison_compiles_one_staged_owner_per_holding_version(self):
        from unbake import runner
        from unbake.layout import split
        from unbake.work import compare

        view, private, _ = self.typedef_contents()
        source = self.project.work / "alpha/alpha.c"
        source.write_text('#include "shared/func_80245618_de_closed.h"\nint alpha(void) { return 1; }\n')
        before = private.read_bytes()
        compiled = []

        @contextmanager
        def compile_unit(project, host, file, version, **kwargs):
            # Follow literal imports exactly as the native preprocessor does.
            seen = set()
            declarations_seen = Counter()
            pending = [(file, file.read_text())]
            import re

            while pending:
                parent, text = pending.pop()
                for alias in re.findall(r"typedef void \(\s*\*([A-Za-z_]\w*)\)\(void\);", text):
                    declarations_seen[alias] += 1
                for name in re.findall(r'#include "([^"]+)"', text):
                    header = next(
                        (home / name for home in (parent.parent, *project.include) if (home / name).is_file()), None
                    )
                    self.assertIsNotNone(header, name)
                    if header not in seen:
                        seen.add(header)
                        pending.append((header, header.read_text()))
            self.assertEqual(declarations_seen["VoidCallback"], 1)
            compiled.append((version, project.include[0]))
            yield self.root / "unit.o"

        with (
            patch.object(compare, "view_for", return_value=view),
            patch.object(runner, "compile_unit", side_effect=compile_unit) as compile_calls,
            patch.object(runner, "link_function", side_effect=_linked) as links,
        ):
            measured = compare.measure(self.project, self.host, source)
        self.assertTrue(measured.exact)
        self.assertEqual(compile_calls.call_count, 2)
        self.assertEqual(links.call_count, 2)
        self.assertEqual(len({home for _, home in compiled}), 1)
        self.assertEqual(private.read_bytes(), before)
        self.assertFalse(compiled[0][1].exists())  # Temporary ownership view is drained.
