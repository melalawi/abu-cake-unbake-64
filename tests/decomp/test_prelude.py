"""m2c prelude typedefs in a published source resolve to the shared types they name."""

import unittest

from unbake.decomp import checks, prelude

SOURCE = """#include "types.h"

typedef s32 M2C_UNK;
typedef s8 M2C_UNK8;
typedef s16 M2C_UNK16;



M2C_UNK func_8020F5A4_de(M2C_UNK16 *arg0, M2C_UNK8 arg1) {
    M2C_UNK temp = *arg0;
    return temp + arg1 + (M2C_UNK_OTHER)0;
}
"""


class PreludeTests(unittest.TestCase):
    def test_typedefs_go_and_each_use_becomes_the_named_type(self) -> None:
        result = prelude.resolve(SOURCE)
        self.assertNotIn("typedef", result)
        self.assertIn("s32 func_8020F5A4_de(s16 *arg0, s8 arg1) {", result)
        self.assertIn("    s32 temp = *arg0;", result)
        self.assertIn("(M2C_UNK_OTHER)0", result)
        self.assertNotIn("\n\n\n", result)
        self.assertEqual([finding.rule for finding in checks.run(result)], ["decompiler-placeholder"])

    def test_a_source_without_the_prelude_is_unchanged(self) -> None:
        self.assertEqual(prelude.resolve("s32 f(void) { return 0; }\n"), "s32 f(void) { return 0; }\n")
