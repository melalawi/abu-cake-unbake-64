import unittest

from unbake.layout import apply


class SpelledNamesTests(unittest.TestCase):
    def test_macro_bodies_count_but_comments_and_includes_do_not(self):
        text = (
            '#include "gone.h"\n#define ONE (*(&value_a + 1))\n/* value_b */\n// value_c\nint f(void) { return value_d; }\n'
        )
        names = apply._spelled(text)
        self.assertLessEqual({"value_a", "value_d"}, names)
        self.assertFalse({"value_b", "value_c", "gone"} & names)


if __name__ == "__main__":
    unittest.main()
