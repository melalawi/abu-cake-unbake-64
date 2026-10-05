"""A field that only carries an address is cracked to void **; a dereferenced one stays unknown."""

import unittest

from unbake.config import Held
from unbake.decomp.draft_layouts import normalize
from unbake.decomp.opaque_pointers import normalize as opaque

NOTE = " /* types.abi.opaque_pointer: field carries an address only */"


class OpaqueFieldTests(unittest.TestCase):
    def test_pointer_fields(self) -> None:
        cases = {
            "write an address": (
                "extern M2C_UNK D_1;\nint f(void *a) {\n    M2C_FIELD(a, M2C_UNK **, 0x14) = &D_1;\n    return 0;\n}\n",
                "extern M2C_UNK D_1;\nint f(void *a) {\n    M2C_FIELD(a, void **, 0x14) = &D_1;"
                + NOTE
                + "\n    return 0;\n}\n",
            ),
            "read into a passed local": (
                "void f(void *a) {\n    void *x;\n    x = M2C_FIELD(a, M2C_UNK **, 4);\n    g(x);\n}\n",
                "void f(void *a) {\n    void *x;\n    x = M2C_FIELD(a, void **, 4);" + NOTE + "\n    g(x);\n}\n",
            ),
            "dereferenced": (
                "int f(void *a) {\n    return *M2C_FIELD(a, M2C_UNK **, 4);\n}\n",
                "int f(void *a) {\n    return *M2C_FIELD(a, M2C_UNK **, 4);\n}\n",
            ),
            "indexed": (
                "int f(void *a) {\n    return M2C_FIELD(a, M2C_UNK **, 4)[1];\n}\n",
                "int f(void *a) {\n    return M2C_FIELD(a, M2C_UNK **, 4)[1];\n}\n",
            ),
            "one transport and one dereferenced": (
                "void f(void *a) {\n    M2C_FIELD(a, M2C_UNK **, 4) = 0;\n    *M2C_FIELD(a, M2C_UNK1 **, 8) = 1;\n}\n",
                "void f(void *a) {\n    M2C_FIELD(a, void **, 4) = 0;"
                + NOTE
                + "\n    *M2C_FIELD(a, M2C_UNK1 **, 8) = 1;\n}\n",
            ),
            "not a pointer field": (
                "void f(void *a) {\n    M2C_FIELD(a, M2C_UNK *, 4) = 0;\n}\n",
                "void f(void *a) {\n    M2C_FIELD(a, M2C_UNK *, 4) = 0;\n}\n",
            ),
        }
        for name, (source, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(opaque(source, ""), expected)

    def test_a_dereferenced_unknown_pointer_field_names_its_offset(self) -> None:
        source = "int f(void *a) {\n    return *M2C_FIELD(a, M2C_UNK **, 0x14);\n}\n"
        with self.assertRaises(Held) as raised:
            normalize(source, "")
        self.assertIn(
            "M2C_UNK: the field at 0x14 is dereferenced but its pointee has no measured layout", str(raised.exception)
        )
