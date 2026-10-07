"""Public compare/fold reuse real anonymous aliases only with complete transitive identity."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.preprocessor import expand
from tests.project_fixture import ProjectCase
from unbake import config, runner
from unbake.config import Held
from unbake.fold import declarations, provider_reuse
from unbake.layout import map as layout_map
from unbake.layout import split
from unbake.layout.header_context import Headers
from unbake.process import named
from unbake.project.headers import include_headers
from unbake.work import compare

FIXTURE = Path(__file__).parent / "fixtures/semantic_provider"
FUNCTION = "func_8025B920_de"


class ProviderSemanticTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def setUp(self):
        super().setUp()
        mapping = layout_map.load(self.project)
        for version in self.project.versions:
            for file in (self.project.version(version).split, self.project.version(version).symbols):
                file.write_text(file.read_text().replace("alpha", FUNCTION))
        groups = tuple(
            replace(group, members=(FUNCTION,) if group.members == ("alpha",) else group.members)
            for group in mapping.groups
        )
        (self.project.root / "layout.toml").write_bytes(layout_map.encoded(replace(mapping, groups=groups)))
        self.project = config.load(self.project.root)
        self.private = self.project.work / FUNCTION / "include/shared/private.h"
        self.private.parent.mkdir(parents=True)
        self.private.write_bytes((FIXTURE / "private.h").read_bytes())
        public = self.project.include[-1]
        self.owner = public / "span/owner.h"
        self.unused = public / "common/unused.h"
        for target, name in ((self.owner, "owner.h"), (self.unused, "unused.h"), (public / "types.h", "types.h")):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((FIXTURE / name).read_bytes())
        self.source = self.project.work / FUNCTION / (FUNCTION + ".c")
        # Keep the real header's original import spelling.
        original = self.private.with_name(FUNCTION + "_closed.h")
        self.private.rename(original)
        self.private = original
        self.source.write_bytes((FIXTURE / (FUNCTION + ".c")).read_bytes())
        self.view = config.draft_view(self.project, FUNCTION)
        self.contents = {p: p.read_text() for p, _ in include_headers(self.view)}

    def assert_layouts(self, headers):
        records = {record.name: record for record in headers.records}
        self.assertEqual(records["SlotCC"].size, 0xCC)
        self.assertEqual(records["RecordD90"].size, 0xD90)
        self.assertEqual(records["Owner_func_8025B5F0_de"].size, 0x108)
        self.assertEqual(records["Header_func_8025B5F0_de"].size, 0x104)
        slot = records["SlotCC"]
        self.assertEqual(
            [field.name for field in slot.fields],
            [
                "index",
                "state",
                "used",
                "pad0",
                "key",
                "pad1",
                "id",
                "pad1b",
                "flag",
                "pad2",
                "mode",
                "value",
                "active",
                "owner",
                "pad3",
            ],
        )
        self.assertEqual(next(field.offset for field in slot.fields if field.name == "owner"), 0xB0)

    def test_public_fold_reuses_all_four_aliases_and_keeps_helper_and_source_bytes(self):
        seen = []

        def folded(project, host, headers, function, text, versions, **kwargs):
            self.assert_layouts(headers)
            seen.append(headers)
            return declarations.Folded(function, text, [], {})

        with (
            patch.object(Headers, "contents", return_value=self.contents) as read,
            patch.object(declarations, "fold_source", side_effect=folded) as fold,
            patch("unbake.layout.structs_fold._prove_includers") as proof,
        ):
            edits = declarations.folded_edits(self.view, self.host, FUNCTION, self.source.read_text(), self.versions)
        self.assertEqual((read.call_count, fold.call_count, proof.call_count), (1, 1, 1))
        self.assertEqual(len(edits), 2)
        header = next(edit for edit in edits if edit.path == self.private)
        self.assertNotIn("typedef struct {", header.after)
        self.assertIn("static __inline__ void release", header.after)
        self.assertIn("slot->owner", header.after)
        self.assertIn('#include "common/unused.h"', header.after)
        self.assertIn('#include "span/owner.h"', header.after)
        source = next(edit for edit in edits if edit.path.suffix == ".c")
        self.assertEqual(source.after, self.source.read_text())
        self.assertEqual(self.private.read_bytes(), (FIXTURE / "private.h").read_bytes())
        self.assertEqual(len(seen), 1)

    def test_public_compare_compiles_each_holding_version_once_after_reuse(self):
        observed = []

        @contextmanager
        def compile_unit(view, host, file, version, **kwargs):
            expanded = expand(file.read_text(), roots=view.include, cwd=file.parent)
            self.assertEqual(expanded.count("struct SlotCC {"), 1)
            self.assertNotIn("typedef struct {", expanded)
            self.assertIn("static __inline__ void release", expanded)
            observed.append((version, view.include[0]))
            yield self.root / "real-boundary.o"

        with (
            patch.object(runner, "compile_unit", side_effect=compile_unit) as compiles,
            patch.object(
                runner, "link_function", side_effect=lambda p, h, o, v, row, f: (split.words(p, row), [])
            ) as links,
        ):
            measured = compare.measure(self.project, self.host, self.source)
        self.assertTrue(measured.exact)
        self.assertEqual((compiles.call_count, links.call_count), (5, 5))
        self.assertEqual([version for version, _ in observed], list(self.versions))
        self.assertFalse(observed[0][1].exists())
        self.assertEqual(self.source.read_bytes(), (FIXTURE / (FUNCTION + ".c")).read_bytes())

    def test_public_compare_reaches_slotcc_after_already_projected_owner_dependencies(self):
        from unbake.layout import redeclarations

        original = self.private.read_text()
        # Preserve the already-classified Header/Owner stage from the real pair;
        # SlotCC/RecordD90 and the inline helper remain the exact real payload.
        names = {"Header_func_8025B5F0_de", "Owner_func_8025B5F0_de"}
        for start, end in reversed(redeclarations.spans(original)):
            row = original[start:end]
            if any(row.rstrip().endswith(name + ";") for name in names):
                original = original[:start] + original[end:]
        original = original.replace('#include "types.h"', '#include "types.h"\n#include "span/owner.h"')
        self.private.write_text(original)
        with (
            patch.object(
                runner,
                "compile_unit",
                side_effect=Held(
                    named("compile.fixture_reached", "compile.fixture_reached", owner="fixture", stage="compile")
                ),
            ) as native,
            patch.object(compare, "view_for", return_value=self.view),
        ):
            measured = compare.measure(self.project, self.host, self.source)
        self.assertEqual(native.call_count, 5)
        self.assertEqual(set(measured.faults), set(self.versions))
        self.assertTrue(all(fault["cause"]["key"] == "compile.fixture_reached" for fault in measured.faults.values()))

    def test_real_semantic_fallback_parses_each_needed_declaration_once_without_io(self):
        from unbake import cdecl

        with (
            patch.object(cdecl, "parse", wraps=cdecl.parse) as parses,
            patch.object(provider_reuse, "_catalog", wraps=provider_reuse._catalog) as catalog,
            patch.object(Path, "read_bytes", side_effect=AssertionError("no payload reread")),
            patch.object(Path, "read_text", side_effect=AssertionError("no payload reread")),
            patch("subprocess.run", side_effect=AssertionError("no native work in ownership proof")) as native,
        ):
            edits = provider_reuse.plan(self.view, self.contents, self.versions)
        self.assertEqual(catalog.call_count, 4)
        self.assertEqual(parses.call_count, 14)
        self.assertEqual(len(edits), 1)
        native.assert_not_called()

    def test_public_compare_holds_genuine_field_width_order_name_qualifier_and_transitive_conflicts(self):
        original = self.private.read_text()
        changes = [
            ("s32 state;", "s16 state;"),
            ("s32 index;\n    s32 state;", "s32 state;\n    s32 index;"),
            ("s32 used;", "s32 occupied;"),
            ("s32 active;", "volatile s32 active;"),
            ("char pad3[0x18];", "char pad3[0x14];"),
            ("s16 samples[20];", "s16 samples[19];"),
            ("s16 local;", "s32 local;"),
        ]
        for old, new in changes:
            with self.subTest(old=old):
                self.private.write_text(original.replace(old, new))
                with patch.object(runner, "compile_unit") as compiler, self.assertRaises(Held) as caught:
                    compare.measure(self.project, self.host, self.source)
                self.assertEqual(caught.exception.key, "headers.declaration.duplicate-shared-provider")
                self.assertIn("shared/", caught.exception.reason)
                self.assertTrue(
                    "common/unused.h" in caught.exception.reason or "span/owner.h" in caught.exception.reason
                )
                compiler.assert_not_called()
        self.private.write_text(original)

    def test_equal_sized_wrong_nominal_owner_is_not_equated_by_shape(self):
        header = self.contents[self.private].replace("Owner_func_8025B5F0_de *owner;", "OtherOwner *owner;")
        header = header.replace(
            "} Owner_func_8025B5F0_de;", "} Owner_func_8025B5F0_de;\ntypedef Owner_func_8025B5F0_de OtherOwner;"
        )
        # Opaque different nominal referent cannot prove the same dependency.
        public = self.contents[self.unused].replace(
            "struct Owner_func_8025B5F0_de *owner;", "struct UnknownOwner *owner;"
        )
        with self.assertRaises(Held):
            provider_reuse.plan(self.view, {**self.contents, self.private: header, self.unused: public}, self.versions)
