"""cdecl.located: a combined unit's parse error names the real file and line through cpp linemarkers."""

import unittest

from unbake import cdecl

UNIT = (
    'int a;\n# 30 "include/span_16E000/code_80405454.h"\nint b;\n'
    '# 10 "src/func_804085E0_de.c"\nvoid f(void);\nMenu *m;\n'
)


class LocatedTests(unittest.TestCase):
    def test_cases(self) -> None:
        for message, expected in [
            (":6:5: before: *", "src/func_804085E0_de.c:11:5: before: *: Menu *m;"),
            (":3:1: before: int", "include/span_16E000/code_80405454.h:30:1: before: int: int b;"),
            (":1:1: before: int", "unit line 1:1: before: int: int a;"),
            ("not a coordinate", "not a coordinate"),
        ]:
            with self.subTest(message):
                self.assertEqual(cdecl.located(UNIT, message), expected)
