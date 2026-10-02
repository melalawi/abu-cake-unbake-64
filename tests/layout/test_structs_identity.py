"""Complete authored layout identity includes offsets, types, sizes and recursive edges."""

import unittest

from unbake.layout.structs import layouts
from unbake.layout.structs_identity import identity, names


class StructIdentityTests(unittest.TestCase):
    def test_recursive_tags_and_aliases_do_not_change_identity(self):
        left = layouts("typedef struct Node { int value; struct Node *next; } Node;")
        right = layouts("typedef struct Link Link; struct Link { int value; Link *next; };")
        self.assertEqual(identity(left[0], left), identity(right[0], right))
        self.assertEqual(names(right, left), {"Link": "Node"})

    def test_conflicting_layout_name_is_stable_and_reused(self):
        old = layouts("struct Owner { int value; };")
        new = layouts("struct Owner { short value; };")
        target = names(new, old)["Owner"]
        self.assertRegex(target, r"^Shape_[0-9a-f]{16}$")
        other = layouts("struct Unrelated { double other; };")
        self.assertEqual(names(new, [*other, *old])["Owner"], target)
        shared = layouts(f"struct {target} {{ short value; }};")
        self.assertEqual(names(new, [*old, *shared])["Owner"], target)

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
                self.assertNotEqual(identity(old[0], old), identity(new[0], new))
        old = layouts("struct Child { int value; }; struct Owner { struct Child *child; };")
        new = layouts("struct Child { float value; }; struct Owner { struct Child *child; };")
        self.assertNotEqual(identity(old[-1], old), identity(new[-1], new))

    def test_equal_layouts_with_different_names_in_one_source_share_a_type(self):
        records = layouts("struct First { int value; }; struct Second { int value; };")
        self.assertEqual(names(records, []), {"First": "First", "Second": "First"})

    def test_array_alias_and_direct_declarator_have_one_layout_identity(self):
        old = layouts("struct First {char data[4];};")
        new = layouts("typedef char Bytes[4]; struct Other {Bytes data;};")
        self.assertNotEqual(old[0].fields[0].extent, new[0].fields[0].extent)
        self.assertEqual(identity(old[0], old), identity(new[0], new))
        self.assertEqual(names(new, old), {"Other": "First"})
