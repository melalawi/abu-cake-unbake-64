"""Route 2 pointer inference changes must not invent an O32 ABI mismatch."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land
from unbake.config import Held
from unbake.typemap import o32


class FuzzyAbiTransportTests(ProjectCase):
    def check(self, expected, source, *, state="known"):
        function = "func_80444148_de"
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
            patch("unbake.typemap.types_db.entries", return_value={function: {"state": state, "prototype": expected}}),
            patch("unbake.typemap.types_db.meta", return_value={}),
        ):
            land._fuzzy_signature(self.project, function, "de", source)

    def test_real_word_entry_accepts_the_older_inferred_pointer(self):
        expected = "int func_80444148_de(int);"
        for pointer in ("void *", "unsigned char *", "struct OlderObject *"):
            with self.subTest(pointer=pointer):
                self.check(expected, "int func_80444148_de(" + pointer + "object) { return object != 0; }")

    def test_pointer_inference_and_return_pointees_use_identical_transport(self):
        self.check(
            "void *func_80444148_de(void *, int);",
            "struct Old *func_80444148_de(struct New *object, int i) { return (struct Old *)object; }",
        )
        self.check(
            "int func_80444148_de(void *);",
            "typedef unsigned int Word; typedef unsigned char *Bytes; int func_80444148_de(Bytes p) {return p != 0;}",
        )

    def test_real_register_width_and_arity_mismatches_remain_refused(self):
        expected = "int func_80444148_de(int);"
        for source in (
            "int func_80444148_de(float p) {return 1;}",
            "int func_80444148_de(long long p) {return 1;}",
            "float func_80444148_de(int p) {return 1;}",
            "long long func_80444148_de(int p) {return 1;}",
            "int func_80444148_de(int p, int q) {return 1;}",
            "int func_80444148_de(int p, ...) {return 1;}",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(Held, "definition differs from canonical"):
                self.check(expected, source)

    def test_unresolved_real_entry_stays_unresolved_without_transport_proof(self):
        function = "func_80429178_us_rev1"
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
            patch("unbake.typemap.types_db.entries", return_value={function: {"state": "unknown"}}),
            self.assertRaisesRegex(Held, "canonical entry signature is unresolved"),
        ):
            land._fuzzy_signature(self.project, function, "us-rev1", f"int {function}(void *p) {{return p != 0;}}")

    def test_aligned_pairs_fp_prefix_and_stack_slots_are_part_of_transport(self):
        self.assertTrue(
            o32.compatible_prototypes("int f(int, int, int, int, void *);", "int f(int, int, int, int, char *);", {})
        )
        for left, right in (
            ("int f(double, int);", "int f(long long, int);"),
            ("int f(int, double);", "int f(int, float);"),
            ("int f(int, long long, int);", "int f(int, int, int);"),
            ("int f(float, ...);", "int f(float);"),
            ("int f();", "int f(void);"),
            ("int f(Unknown);", "int f(int);"),
        ):
            with self.subTest(left=left, right=right):
                self.assertFalse(o32.compatible_prototypes(left, right, {}))
