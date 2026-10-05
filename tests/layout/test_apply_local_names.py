"""A source's own declaration replaces the header import only when it precedes every use."""

import pickle
import unittest

from unbake.config import Held, Unfinished
from unbake.layout.apply import _local_names


class LocalNamesTests(unittest.TestCase):
    def test_declared_before_or_after_first_use(self) -> None:
        for name, text, expected in (
            ("declared before use", "extern int D_1;\nint f(void) { return D_1; }\n", {"D_1"}),
            (
                "used before a later declaration",
                "int f(void) { return D_1; }\nextern int D_1;\nint g(void) { return D_1; }\n",
                set(),
            ),
            ("a comment is not a use", "/* D_1 */\nextern int D_1;\nint f(void) { return D_1; }\n", {"D_1"}),
            (
                "a declarator split by version conditionals",
                "extern void\n#if defined(V_A)\nD_1\n#else\nD_2\n#endif\n(int a);\nint f(void) { D_1(0); }\n",
                {"D_1"},
            ),
        ):
            with self.subTest(name):
                self.assertEqual(_local_names(text) & {"D_1"}, expected)


class HeldPickleTests(unittest.TestCase):
    def test_refusals_cross_process_boundaries_intact(self) -> None:
        for held in (Held("compile", "cc1 exited 1: x", next_action="fix"), Unfinished("solve", "types.thing")):
            with self.subTest(type(held).__name__):
                back = pickle.loads(pickle.dumps(held))
                self.assertEqual(
                    (type(back), back.phase, back.reason, back.next_action, back.args),
                    (type(held), held.phase, held.reason, held.next_action, held.args),
                )
