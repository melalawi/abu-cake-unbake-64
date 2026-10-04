import unittest

from unbake.layout import apply


class SpelledNamesTests(unittest.TestCase):
    def test_macro_bodies_count_but_comments_and_includes_do_not(self):
        text = "\n".join(
            [
                '#include "gone.h"',
                "#define ONE (*(&value_a + 1))",
                "/* value_b */",
                "// value_c",
                "int f(void) { return value_d; }",
            ]
        )
        names = apply.spelled(text)
        self.assertLessEqual({"value_a", "value_d"}, names)
        self.assertFalse({"value_b", "value_c", "gone"} & names)


if __name__ == "__main__":
    unittest.main()
