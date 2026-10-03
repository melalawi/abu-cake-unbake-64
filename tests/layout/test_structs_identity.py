"""Complete authored layout identity includes offsets, types, sizes and recursive edges."""

import unittest

from unbake.layout.structs import layouts
from unbake.layout.structs_identity import Index, identity


def ident(record, records):
    return identity(record, {item.name: item for item in records})


class StructIdentityTests(unittest.TestCase):
    def test_recursive_tags_and_aliases_do_not_change_identity(self):
        left = layouts("typedef struct Node { int value; struct Node *next; } Node;")
        right = layouts("typedef struct Link Link; struct Link { int value; Link *next; };")
        self.assertEqual(ident(left[0], left), ident(right[0], right))

    def test_size_offsets_signedness_bitfields_and_pointees_are_evidence(self):
        cases = (
            ("int value;", "unsigned int value;"),
            ("int value;", "int value; int tail;"),
            ("int value;", "char pad[4]; int value;"),
            ("unsigned int value:3;", "unsigned int value:4;"),
        )
        for left, right in cases:
            with self.subTest(right=right):
                old = layouts(f"struct Owner {{ {left} }};")
                new = layouts(f"struct Owner {{ {right} }};")
                self.assertNotEqual(ident(old[0], old), ident(new[0], new))
        old = layouts("struct Child { int value; }; struct Owner { struct Child *child; };")
        new = layouts("struct Child { float value; }; struct Owner { struct Child *child; };")
        self.assertNotEqual(ident(old[-1], old), ident(new[-1], new))

    def test_array_alias_and_direct_declarator_have_one_layout_identity(self):
        old = layouts("struct First {char data[4];};")
        new = layouts("typedef char Bytes[4]; struct Other {Bytes data;};")
        self.assertNotEqual(old[0].fields[0].extent, new[0].fields[0].extent)
        self.assertEqual(ident(old[0], old), ident(new[0], new))


class PointeeProviderTests(unittest.TestCase):
    def test_reused_parent_keeps_its_pointee_provider(self):
        for reversed_order in (False, True):
            with self.subTest(reversed_order=reversed_order):
                existing = layouts(
                    "struct Alpha {int active; int count;};"
                    "struct Zed {int first; int second;};"
                    "struct Track {struct Zed *bank; int state;};"
                )
                local = layouts("struct Bank {int a; int b;}; struct Track {struct Bank *bank; int state;};")
                if reversed_order:
                    local.reverse()
                resolution = Index(existing).resolve(local, "owner")
                self.assertEqual(resolution["Track"][0], "Track")
                self.assertEqual(resolution["Bank"][0], "Zed")

    def test_conflicting_pointee_structure_is_not_reused(self):
        existing = layouts("struct Bank {float value;}; struct Track {struct Bank *bank;};")
        local = layouts("struct Bank {int value;}; struct Track {struct Bank *bank;};")
        resolution = Index(existing).resolve(local, "owner")
        self.assertEqual(resolution["Track"][0], "Track_owner")
        self.assertEqual(resolution["Bank"][0], "Bank_owner")
