"""Guarded split providers retain compiler visibility and authored edit offsets."""

import unittest
from pathlib import Path

from unbake.decomp.header_declarations import declaration_source
from unbake.layout.header_context import Headers, context, guarded_source, header_guard
from unbake.layout.split import Edit
from unbake.project.config import Held


class HeaderContextTests(unittest.TestCase):
    def test_new_header_guard_does_not_shadow_a_different_declaration_provider(self):
        for root in (Path("/original"), Path("/copied")):
            for existing in (False, True):
                with self.subTest(root=root, existing=existing):
                    destination = root / "include/shared/alpha.h"
                    texts = (
                        {
                            root / "include/shared/decls/alpha.h": (
                                "#ifndef UNBAKE_ALPHA_H\n#define UNBAKE_ALPHA_H\nextern int alpha(void);\n#endif\n"
                            )
                        }
                        if existing
                        else {}
                    )
                    headers = Headers(texts, root=root)
                    guard = header_guard(headers, destination)
                    self.assertEqual(guard == "UNBAKE_ALPHA_H", not existing)
                    headers.apply(
                        [
                            Edit(
                                destination,
                                "",
                                f"#ifndef {guard}\n#define {guard}\nstruct Owner {{int value;}};\n#endif\n",
                                (),
                            )
                        ]
                    )
                    self.assertIn("Owner", headers.index.names)
                    if existing:
                        original = Headers(
                            {
                                Path("/original/include/shared/decls/alpha.h"): texts[
                                    root / "include/shared/decls/alpha.h"
                                ]
                            },
                            root=Path("/original"),
                        )
                        self.assertEqual(guard, header_guard(original, Path("/original/include/shared/alpha.h")))

    def test_guarded_wrappers_and_nested_generated_copies_share_one_provider(self):
        for kind in ("struct", "union"):
            for generated_first in (False, True):
                with self.subTest(kind=kind, generated_first=generated_first):
                    body = (
                        "#ifndef AUTHORED_H\n#define AUTHORED_H\n"
                        f"typedef {kind} Record Record;\n{kind} Record {{ int value; }};\n#endif\n"
                    )
                    wrapper = Path("/project/include/wrapper.h")
                    generated = Path("/project/include/generated.h")
                    texts = {wrapper: body, generated: "#ifndef GENERATED_H\n#define GENERATED_H\n" + body + "#endif\n"}
                    if generated_first:
                        texts = dict(reversed(list(texts.items())))
                    headers = Headers(texts, root=Path("/project"))
                    self.assertEqual(headers.texts, texts)
                    self.assertEqual(len(headers.records), 1)
                    record = headers.records[0]
                    self.assertEqual(record.size, 4)
                    self.assertIn(f"{kind} Record {{", headers.source[record.start : record.end])
                    self.assertEqual(headers.homes["Record"], next(iter(headers.texts)))
                    self.assertEqual(headers.homes[f"{kind} Record"], next(iter(headers.texts)))

    def test_guard_mask_preserves_offsets_enums_macros_else_and_undef(self):
        for kind in ("struct", "union", "enum"):
            with self.subTest(kind=kind):
                body = (
                    "#ifndef COPY_H\n#define COPY_H\n#define COUNT 2\n"
                    f"{kind} Record {{ value }};\n#else\nextern int repeated;\n#endif\n"
                )
                source = body + body.replace("COUNT 2", "COUNT 9")
                masked = guarded_source(source)
                self.assertEqual(len(masked), len(source))
                self.assertEqual(masked.count("\n"), source.count("\n"))
                active = declaration_source(masked)
                self.assertEqual(active.count(f"{kind} Record"), 1)
                self.assertEqual(active.count("extern int repeated"), 1)
                self.assertNotIn("COUNT 9", masked)
                reset = guarded_source(body + "#undef COPY_H\n" + body)
                self.assertEqual(declaration_source(reset).count(f"{kind} Record"), 2)

    def test_real_duplicates_name_both_provider_locations(self):
        for kind in ("struct", "union"):
            with self.subTest(kind=kind):
                with self.assertRaises(Held) as caught:
                    context(
                        {
                            Path("/p/a.h"): f"{kind} Record {{int a;}};\n",
                            Path("/p/b.h"): f"\n{kind} Record {{int b;}};\n",
                        },
                        root=Path("/p"),
                    )
                self.assertIn("duplicate definition", caught.exception.reason)
                self.assertIn("providers: a.h:1, b.h:2", caught.exception.reason)

    def test_split_alias_and_definition_have_distinct_homes_and_value_order(self):
        root = Path("/p")
        texts = {
            root / "owner.h": "struct Owner { Record record; char pad[sizeof(Record)]; };",
            root / "alias.h": "typedef struct Record Record;",
            root / "layout.h": "struct Record {int value;};",
        }
        headers = Headers(texts, root=root)
        self.assertEqual(headers.homes["Record"], root / "alias.h")
        self.assertEqual(headers.homes["struct Record"], root / "layout.h")
        self.assertEqual(next(r.size for r in headers.records if r.name == "Owner"), 8)

    def test_failed_multi_edit_and_existing_edit_leave_context_unchanged(self):
        root = Path("/p")
        original = {root / "base.h": "struct Base {int value;};"}
        for edit_existing in (False, True):
            with self.subTest(edit_existing=edit_existing):
                headers = Headers(original, root=root)
                first = Edit(root / "added.h", "", "struct Added {int value;};", ())
                path = root / "base.h" if edit_existing else root / "broken.h"
                second = Edit(path, original.get(path, ""), "struct Broken {Missing value;};", ())
                with self.assertRaises(Held):
                    headers.apply([first, second])
                self.assertEqual(headers.texts, original)
                self.assertNotIn("struct Added", headers.types)
                _, records = headers.parse("struct Later {struct Base base;};")
                self.assertEqual(records[0].size, 4)

    def test_incremental_append_obeys_existing_guard_and_keeps_new_records(self):
        path = Path("/p/base.h")
        body = "#ifndef BASE_H\n#define BASE_H\nstruct Base {int value;};\n#endif\n"
        headers = Headers({path: body}, root=path.parent)
        headers.apply([Edit(Path("/p/copy.h"), "", body + "struct Added {struct Base base;};", ())])
        self.assertEqual([r.name for r in headers.records], ["Base", "Added"])
        self.assertEqual(headers.homes["struct Base"], path)
