"""A source's own declaration replaces the header import only when it precedes every use."""

import pickle
import unittest
from pathlib import Path

from unbake.config import Held, Unfinished
from unbake.layout import redeclarations
from unbake.layout.apply import _local_names
from unbake.process import Action, named
from unbake.typemap.database import source_private


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
                self.assertEqual(_local_names(Path("src/f.c"), text) & {"D_1"}, expected)


class HeldPickleTests(unittest.TestCase):
    def test_refusals_cross_process_boundaries_intact(self) -> None:
        for held in (
            Held(
                named(
                    "fixture.refusal",
                    "cc1 exited 1: x",
                    owner="fixture",
                    stage="compile",
                    action=Action("edit", paths=("src/f.c",)),
                )
            ),
            Unfinished("solve", "types.thing"),
        ):
            with self.subTest(type(held).__name__):
                back = pickle.loads(pickle.dumps(held))
                self.assertEqual(
                    (type(back), back.phase, back.reason, back.fault.cause.action, back.args),
                    (type(held), held.phase, held.reason, held.fault.cause.action, held.args),
                )


class DeclarationRefusalTests(unittest.TestCase):
    def test_refusal_names_source_and_symbol(self) -> None:
        with self.assertRaises(Held) as caught:
            redeclarations.parse(Path("src/func_1.c"), "extern int D_1 D_2;")
        self.assertEqual(caught.exception.fault.cause.owner, "cdecl")
        self.assertIn("D_2", caught.exception.reason)


class SourcePrivateTests(unittest.TestCase):
    def test_declarations_spelling_source_only_types_stay_local(self) -> None:
        for name, declaration, expected in (
            ("source typedef", "extern Handler D_1;", True),
            ("source tag", "extern struct Local *D_1;", True),
            ("header types only", "extern s32 D_1;", False),
        ):
            with self.subTest(name):
                self.assertEqual(source_private(declaration, {"Local"}, {"Handler"}), expected)
