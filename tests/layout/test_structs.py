import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from unbake.decomp import needs
from unbake.layout import split_apply, structs
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held, Project


class DeclarationTests(unittest.TestCase):
    def test_padding_extents_use_target_sizeof(self) -> None:
        for expression, expected in [
            ("0x10 - 0x4", 12),
            ("0x10 - sizeof(u8)", 15),
            ("0x10 - sizeof(unsigned long)", 12),
            ("0x10 - sizeof(void *)", 12),
            ("0x10 - sizeof(Bytes)", 13),
            ("0x10 - sizeof(u16[3])", 10),
            ("0x10 - sizeof(struct Pair)", 8),
            ("0x10 - sizeof(Word)", 12),
        ]:
            with self.subTest(expression=expression):
                records = layouts(
                    "typedef u8 Bytes[3]; typedef u32 Word; struct Pair { u8 x; u32 y; };"
                    f"struct Padded {{ char pad[{expression}]; u8 tail; }};"
                )
                padded = records[-1]
                self.assertEqual(padded.fields[0].extent, (expected,))
                self.assertEqual(padded.fields[1].offset, expected)
                self.assertEqual(padded.size, expected + 1)
        for expression, reason in [("sizeof(Missing)", "Missing"), ("sizeof(void)", "void")]:
            with self.subTest(expression=expression), self.assertRaisesRegex(Held, reason):
                layouts(f"struct Padded {{ char pad[{expression}]; }};")

    def test_integer_extents(self) -> None:
        for expression, count in [
            ("SLOT_COUNT", 8),
            ("PAIRS", 16),
            ("(1U << 3)", 8),
            ("0x10UL / 2", 8),
            ("010", 8),
            ("~(-9)", 8),
        ]:
            with self.subTest(expression=expression):
                source = (
                    "#define SLOT_COUNT 8\n#define PAIRS (SLOT_COUNT * 2)\n"
                    + f"typedef struct Entry {{ s32 id; s16 picks[{expression}]; }} Entry; "
                    "typedef struct Roster { Entry entries[SLOT_COUNT]; } Roster;"
                )
                entry, roster = layouts(source)
                self.assertEqual(entry.size, 4 + 2 * count)
                self.assertEqual(roster.size, entry.size * 8)
                self.assertEqual(roster.fields[0].extent, (8,))

    def test_alignment_and_declarators(self) -> None:
        cases = [
            ("u8 a; u16 b; u32 c; u64 d;", 16, [0, 2, 4, 8]),
            ("u8 a[3]; u32 b;", 8, [0, 4]),
            ("void (*cb)(int value); void (*slots[3])(void);", 16, [0, 4]),
            ("Missing *ptr; u16 (*matrix)[8];", 8, [0, 4]),
            ("u16 *matrix[8];", 32, [0]),
            ("u8 bytes[2][3];", 6, [0]),
        ]
        for declarations, size, offsets in cases:
            with self.subTest(declarations=declarations):
                layout = layouts(f"struct Record {{ {declarations} }};")[0]
                self.assertEqual(layout.size, size)
                self.assertEqual([item.offset for item in layout.fields], offsets)

    def test_comma_members_preserve_each_declarator(self) -> None:
        source = (
            "struct Record { s16 x1, y1, x2, y2; u32 *p, a[2], (*cb)(int, int); unsigned a1:3, :2, a2:5; u8 tail; };"
        )
        record = layouts(source)[0]
        self.assertEqual(record.size, 28)
        self.assertEqual([item.offset for item in record.fields], [0, 2, 4, 6, 8, 12, 20, 24, 24, 24, 26])
        self.assertEqual([(item.bit_offset, item.bit_size) for item in record.fields[7:10]], [(0, 3), (3, 2), (5, 5)])
        rebuilt = layouts("struct Record {" + " ".join(item.declaration for item in record.fields) + "};")[0]
        self.assertEqual(
            [(item.name, item.offset, item.size) for item in rebuilt.fields],
            [(item.name, item.offset, item.size) for item in record.fields],
        )
        with self.assertRaisesRegex(Held, r"bad.*line 2"):
            layouts("struct Broken {\n u32 good, bad @;\n};")

    def test_union_and_typedef_order(self) -> None:
        source = (
            "typedef struct Later Later; typedef Later Alias; "
            "typedef union View { u8 v0; f32 v1; s8 v2; } View; "
            "struct Record { u8 pad[0x1C]; View unk1C; union { u32 bits; "
            "struct { u16 lo; u16 hi; }; }; Later *next; }; "
            "struct Later { s32 x; }; typedef char Check[(sizeof(Record) == 0x28) ? 1 : -1];"
        )
        records = {record.name: record for record in layouts(source)}
        self.assertEqual(records["Record"].size, 0x28)
        self.assertEqual(records["Record"].fields[1].offset, 0x1C)
        self.assertEqual(
            [(item.name, item.offset) for item in records["View"].fields], [("v0", 0), ("v1", 0), ("v2", 0)]
        )
        self.assertIn("Alias", records["Later"].aliases)
        self.assertEqual(records["Record"].fields[2].fields[1].fields[1].offset, 2)

    def test_named_refusals(self) -> None:
        cases = [
            ("struct X { u8 bytes[MISSING]; };", "MISSING"),
            ("#define A B\n#define B A\nstruct X { u8 bytes[A]; };", "A"),
            ("struct X { Missing value; };", "Missing"),
            ("struct X { u8 bytes[-1]; };", "bytes"),
            ("struct X { u8 bytes[1/0]; };", "1/0"),
            ("struct X { u8 bytes[call()]; };", "call()"),
            ("struct X { u32 bits:33; };", "bits"),
            ("struct X { struct X value; };", "X"),
            ("struct X { void cb(void); };", "void"),
            ("struct X { u8 a;", "}"),
            ('#include "missing.h"\n', "preprocessing"),
            ("#if VERSION_US\nstruct X { u32 x; };\n#endif", "preprocessing"),
        ]
        for source, name in cases:
            with self.subTest(source=source):
                with self.assertRaises(Held) as caught:
                    layouts(source)
                self.assertIn(name, str(caught.exception))
        self.assertEqual(layouts(""), [])

    def test_project_preprocessing(self) -> None:
        cpp = shutil.which("cpp")
        self.assertIsNotNone(cpp, "cpp executable required")
        assert cpp is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            include = root / "include"
            include.mkdir()
            (include / "count.h").write_text("#define SLOT_COUNT 8\n")
            source = root / "source.c"
            source.write_text('#include "count.h"\n#if ACTIVE\nstruct Roster { u32 slots[SLOT_COUNT]; };\n#endif\n')
            project = SimpleNamespace(
                root=root,
                include=(include,),
                compiler_for=lambda path: SimpleNamespace(cflags=("-DOTHER=1",)),
                version=lambda version: SimpleNamespace(macros=("ACTIVE=1",)),
            )
            policy = SimpleNamespace(cpp=Path(cpp), cppflags=("-undef", "-nostdinc"))
            self.assertEqual(layouts(source, project=project, policy=policy, version="us")[0].size, 32)
            for kwargs, missing in [
                ({}, "project"),
                ({"project": project}, "policy"),
                ({"project": project, "policy": policy}, "VERSION"),
            ]:
                with self.subTest(missing=missing):
                    with self.assertRaises(Held) as caught:
                        layouts(source, **cast(dict[str, Any], kwargs))
                    self.assertIn(missing, str(caught.exception))


class FoldTests(unittest.TestCase):
    def test_registered_layouts_round_trip_and_fold(self) -> None:
        self.assertIn((needs.LayoutNeed, 30, structs.resolve), needs.resolvers())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            include = root / "include"
            include.mkdir()
            header = include / "record.h"
            header.write_text("struct Record { char pad[4]; };")
            source = root / "draft.c"
            source.write_text("struct Record { unsigned int value; };")
            project = SimpleNamespace(
                root=root,
                include=(include,),
                versions=("us", "eu"),
                compiler_for=lambda path: SimpleNamespace(cflags=()),
                version=lambda version: SimpleNamespace(
                    macros=(), split=root / (version + ".yaml"), symbols=root / (version + ".txt")
                ),
                src=root / "src",
            )
            policy = SimpleNamespace(cpp=Path(cast(str, shutil.which("cpp"))), cppflags=("-undef", "-nostdinc"))
            pending = [
                needs.decode(row)
                for row in json.loads(
                    json.dumps(
                        [
                            needs.encode(
                                needs.LayoutNeed(
                                    version,
                                    record.name,
                                    record.fields,
                                    str(source),
                                    dict(
                                        kind=record.kind,
                                        size=record.size,
                                        alignment=record.alignment,
                                        aliases=record.aliases,
                                    ),
                                )
                            )
                            for version in project.versions
                            for record in layouts(source, project=project, policy=policy, version=version)
                        ],
                        default=lambda value: value.__dict__,
                    )
                )
            ]
            edits = structs.resolve(pending, project, policy)
            self.assertEqual(len(edits), 1)
            self.assertEqual(edits[0].versions, ("us", "eu"))
            split_apply._write_staging(cast(Project, project), edits)
            self.assertIn("unsigned int value;", header.read_text())
            self.assertEqual(structs.resolve(pending, project, policy), [])

    def test_player_padding_and_union_views(self) -> None:
        draft = layouts(
            "typedef struct SharedPlayer { u8 pad0[3]; u8 team; char pad4[0x14]; st"
            "ruct Body *body; } SharedPlayer; typedef SharedPlayer Player;"
        )
        cases = [
            ("typedef struct SharedPlayer SharedPlayer; struct SharedPlayer { char pad0[0x1C]; };", "Body"),
            (
                (
                    "typedef struct SharedPlayer SharedPlayer; struct SharedPlayer { union "
                    "{ u8 bytes[0x18]; }; union { char *track; } views18; };"
                ),
                "track",
            ),
        ]
        for text, preserved in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                header = root / "player.h"
                header.write_text(text)
                edits = fold(draft, root, versions=("us", "eu"))
                self.assertEqual(len(edits), 1)
                self.assertEqual(header.read_text(), text)
                self.assertIn("u8 team;", edits[0].after)
                self.assertIn("struct Body *body;", edits[0].after)
                self.assertIn(preserved, edits[0].after)
                self.assertEqual(edits[0].versions, ("us", "eu"))
                self.assertEqual(layouts(edits[0].after)[0].size, 0x1C)
                header.write_text(edits[0].after)
                self.assertEqual(fold(draft, root, versions=("us",)), [])

    def test_fold_comma_list_keeps_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / "record.h"
            header.write_text("struct Record { char pad[4], tail[4]; };")
            draft = layouts("struct Record { u16 first, second; char tail[4]; };")
            edits = fold(draft, root, versions=("us",))
            parsed = layouts(edits[0].after)[0]
            self.assertEqual(
                [(item.name, item.offset) for item in parsed.fields], [("first", 0), ("second", 2), ("tail", 4)]
            )
            self.assertEqual(parsed.size, 8)
            header.write_text("struct Record { char pad0[4], pad4[4], tail[4]; };")
            edits = fold(layouts("struct Record { u32 a, b; char tail[4]; };"), root, versions=("us",))
            self.assertEqual(
                [(item.name, item.offset) for item in layouts(edits[0].after)[0].fields],
                [("a", 0), ("b", 4), ("tail", 8)],
            )

    def test_guarded_scalar_home_and_comma_edits_compile_as_c89(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "types.h").write_text("typedef int s32;\n")
            (root / "base.h").write_text("#ifndef BASE_H\n#define BASE_H\ntypedef int s32;\n#endif\n")
            header = root / "existing.h"
            header.write_text('#include "base.h"\nstruct Existing { char pad[4], tail[4]; };\n')
            project = SimpleNamespace(include=(root,), versions=("us",))
            records = layouts("struct Existing { s32 value; char tail[4]; }; struct Added { s32 x, y; };")
            edits = fold(records, project)
            self.assertEqual(len(edits), 2)
            for edit in edits:
                edit.path.write_text(edit.after)
            self.assertIn('#include "base.h"', (root / "structs.h").read_text())
            source = root / "test.c"
            source.write_text('#include "base.h"\n#include "existing.h"\n#include "structs.h"\n')
            compiler = shutil.which("cc")
            self.assertIsNotNone(compiler)
            assert compiler is not None
            result = subprocess.run(
                [compiler, "-std=c89", "-pedantic-errors", "-fsyntax-only", str(source)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(layouts(header.read_text().split("\n", 1)[1])[0].size, 8)

    def test_shared_header_dependency_order_compiles_and_is_stable(self) -> None:
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        assert compiler is not None
        records = layouts(
            "typedef struct Owner Owner; typedef struct Leaf Leaf; typedef union View View; "
            "struct Owner { Leaf values[2]; View views[1]; "
            "struct Leaf *next; }; struct Leaf { Owner *owner; int value; }; union View { int value; };"
        )
        for existing in (
            "",
            "typedef struct Owner Owner; struct Owner { char pad[24]; };\n"
            "struct Unrelated { int value; }; typedef struct Unrelated Unrelated;\n"
            "struct Consumer { Unrelated *value; }; typedef struct Consumer Consumer;\n",
        ):
            with self.subTest(existing=bool(existing)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                header = root / "structs.h"
                if existing:
                    header.write_text(existing)
                project = SimpleNamespace(include=(root,), versions=("us", "eu"))
                edits = fold(records, project)
                self.assertEqual(len(edits), 1)
                generated = edits[0].after
                self.assertLess(generated.index("struct Leaf {"), generated.index("struct Owner {"))
                self.assertLess(generated.index("union View {"), generated.index("struct Owner {"))
                # Evidence order must not change the generated bytes.
                self.assertEqual(fold(list(reversed(records)), project)[0].after, generated)
                header.write_text(generated)
                result = subprocess.run(
                    [compiler, "-std=c89", "-pedantic-errors", "-fsyntax-only", "-x", "c", str(header)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                rebuilt = {
                    record.name: record
                    for record in layouts(
                        generated.replace("#ifndef UNBAKE_STRUCTS_H\n#define UNBAKE_STRUCTS_H\n", "").replace(
                            "#endif", ""
                        )
                    )
                }
                for record in records:
                    self.assertEqual(rebuilt[record.name].size, record.size)
                    self.assertEqual(
                        [(field.name, field.offset, field.size) for field in rebuilt[record.name].fields],
                        [(field.name, field.offset, field.size) for field in record.fields],
                    )
                self.assertEqual(fold(records, project), [])

    def test_existing_union_and_forward_typedef_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = (
                "typedef struct Settings Settings; typedef Settings Alias; typedef unio"
                "n View { u8 v0; f32 v1; } View; struct Settings { char pad[0x20]; View"
                " value; };"
            )
            header = root / "settings.h"
            header.write_text(text)
            draft = layouts(
                "typedef struct Settings Settings; typedef union View { u8 v0; f32 v1; "
                "} View; struct Settings { char pad[0x20]; View value; };"
            )
            self.assertEqual(fold(draft, root, versions=("us",)), [])
            self.assertEqual(header.read_text(), text)

    def test_fold_named_refusals(self) -> None:
        cases = [
            ("struct X { u32 value; };", "struct X { u16 value; };", "X.value"),
            ("struct X { u32 value; };", "struct X { char pad[4]; u32 value; };", "X.value"),
            ("struct X { u32 value; };", "struct X { f32 other; };", "X.other"),
            ("struct X { u32 value; };", "union X { f32 value; };", "X"),
            ("struct X { u32 value; };", "struct Y { u32 value; };", "Y"),
        ]
        for header_text, draft, name in cases:
            with self.subTest(name=name, draft=draft), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "types.h").write_text(header_text)
                with self.assertRaises(Held) as caught:
                    fold(layouts(draft), root, versions=("us",))
                self.assertIn(name, str(caught.exception))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(Held) as caught:
                fold([], root)
            self.assertIn("versions", str(caught.exception))
            with self.assertRaises(Held) as caught:
                fold([], root, versions=("us",))
            self.assertIn("headers", str(caught.exception))

    def test_fold_subset_reuses_existing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / "record.h"
            text = "struct Record { u32 first; u32 second; u32 third; };"
            header.write_text(text)
            for draft in ("struct Record { u32 first; };", "struct Record { u32 inferred; };"):
                self.assertEqual(fold(layouts(draft), root, versions=("us",)), [])
            self.assertEqual(header.read_text(), text)

    def test_fold_subset_extends_only_unused_padding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / "record.h"
            header.write_text("struct Record { u32 first; char pad[4]; u32 retained; };")
            edits = fold(layouts("struct Record { u32 first; u32 second; };"), root, versions=("us",))
            self.assertEqual(len(edits), 1)
            fields = layouts(edits[0].after)[0].fields
            self.assertEqual(
                [(field.name, field.offset) for field in fields], [("first", 0), ("second", 4), ("retained", 8)]
            )

    def test_sdk_union_subset_and_duplicate_typedef_survive_additions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "n64sdk.h"
            sdk_text = (
                "typedef struct { u32 w0; u32 w1; } Gwords; typedef union { Gwords words; u64 force_alignment; } Gfx;"
            )
            sdk.write_text(sdk_text)
            inferred = root / "view.h"
            inferred_text = "typedef struct { u32 w0; u32 w1; } Gfx;"
            inferred.write_text(inferred_text)
            project = SimpleNamespace(include=(root,), versions=("us",))
            subset = layouts("typedef struct { u32 w0; } Gwords; typedef struct { Gwords words; } Gfx;")
            self.assertEqual(fold(subset, project), [])
            edits = fold(subset + layouts("struct Added { int value; };"), project)
            self.assertEqual([edit.path.name for edit in edits], ["structs.h"])
            self.assertEqual(sdk.read_text(), sdk_text)
            self.assertEqual(inferred.read_text(), inferred_text)

    def test_fold_extends_implicit_gap_and_tail(self) -> None:
        cases = [
            (
                "struct Record { u8 first; u32 retained; };",
                "struct Record { u8 first; char pad[1]; u16 second; };",
                8,
                2,
            ),
            ("struct Record { u32 first; };", "struct Record { u32 first; u32 second; };", 8, 4),
        ]
        for existing, draft, size, offset in cases:
            with self.subTest(draft=draft), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                header = root / "record.h"
                header.write_text(existing)
                edits = fold(layouts(draft), root, versions=("us",))
                result = layouts(edits[0].after)[0]
                self.assertEqual(result.size, size)
                self.assertEqual(next(field.offset for field in result.fields if field.name == "second"), offset)
                header.write_text(edits[0].after)
                self.assertEqual(fold(layouts(draft), root, versions=("us",)), [])

    def test_fold_conflict_names_both_members_and_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / "n64sdk.h"
            header.write_text("typedef union { u32 words; u64 alignment; } Gfx;")
            draft_header = root / "draft.h"
            draft_header.write_text("typedef struct { f32 inferred; } Gfx;")
            records = [replace(record, source=str(draft_header)) for record in layouts(draft_header.read_text())]
            # The draft is evidence, not an existing include header.
            with self.assertRaises(Held) as caught:
                fold(records, [header], versions=("us",))
            message = str(caught.exception)
            for value in ("Gfx.inferred", "Gfx.words", str(header), str(draft_header)):
                self.assertIn(value, message)
            self.assertIn("offset/type/size", message)

    def test_fold_multiple_fields_in_one_implicit_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "record.h").write_text("struct Record { u8 first; u32 retained; };")
            edits = fold(layouts("struct Record { u8 first; u8 second; u16 third; };"), root, versions=("us",))
            self.assertEqual(
                [(field.name, field.offset) for field in layouts(edits[0].after)[0].fields],
                [("first", 0), ("second", 1), ("third", 2), ("retained", 4)],
            )

    def test_sdk_extension_uses_shared_tag_and_keeps_sdk_typedef(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "n64sdk.h"
            text = "typedef struct { unsigned int used; char pad[4]; } Sdk;"
            sdk.write_text(text)
            project = SimpleNamespace(include=(root,), versions=("us",))
            draft = layouts("typedef struct { unsigned int used; unsigned int added; } Sdk;")
            edits = fold(draft, project)
            self.assertEqual([edit.path.name for edit in edits], ["structs.h"])
            self.assertIn("struct Sdk {", edits[0].after)
            self.assertNotIn("typedef struct Sdk Sdk;", edits[0].after)
            self.assertEqual(sdk.read_text(), text)
            edits[0].path.write_text(edits[0].after)
            self.assertEqual(fold(draft, project), [])
            compiler = shutil.which("cc")
            assert compiler is not None
            result = subprocess.run(
                [compiler, "-std=c89", "-pedantic-errors", "-fsyntax-only", "-x", "c", "-"],
                input=text + "\n" + edits[0].after + "\nSdk original; struct Sdk extended;\n",
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_fold_extends_nested_padding_without_removing_union_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / "record.h"
            header.write_text(
                "struct Record { u32 first; union { struct { u32 used; char pad[4]; } words; "
                "u64 raw; } view; u32 retained; };"
            )
            draft = layouts(
                "struct Record { u32 first; char pad4[4]; struct { struct { u32 used; u32 added; } words; } view; };"
            )
            edits = fold(draft, root, versions=("us",))
            result = layouts(edits[0].after)[0]
            self.assertEqual(result.size, 24)
            self.assertIn("u64 raw;", edits[0].after)
            self.assertIn("u32 retained;", edits[0].after)
            self.assertEqual(result.fields[1].fields[0].fields[1].name, "added")
            header.write_text(edits[0].after)
            self.assertEqual(fold(draft, root, versions=("us",)), [])

    def test_cross_header_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "types.h").write_text(
                "typedef struct Later Later; typedef Later Alias; struct Later { u32 value; };"
            )
            (root / "container.h").write_text("struct Container { Alias *next; char pad[4]; };")
            draft = layouts("typedef struct Later Later; struct Container { Later *next; u32 flags; };")
            edits = fold(draft, root, versions=("us",))
            self.assertEqual(len(edits), 1)
            self.assertIn("u32 flags;", edits[0].after)


if __name__ == "__main__":
    unittest.main()
