"""The types method proposes integer spelling changes of the drafted function only."""

import time
import unittest
from types import SimpleNamespace

from unbake.config import Held
from unbake.search import types as method

PRE = (
    "typedef signed char s8;\ntypedef unsigned char u8;\ntypedef short s16;\ntypedef unsigned short u16;\n"
    "typedef int s32;\n"
)


def proposals(source: str, seconds: float = 30.0) -> list[method.Mutation]:
    trial = SimpleNamespace(function="f")
    found = method.propose(PRE + source, trial, SimpleNamespace(deadline=time.monotonic() + seconds))  # type: ignore[arg-type]
    return [method.Mutation(m.kind, m.description, m.source.removeprefix(PRE)) for m in found]


def texts(source: str) -> list[str]:
    return [mutation.source for mutation in proposals(source)]


class TypesTests(unittest.TestCase):
    def test_signedness_flips_come_first_then_widths(self) -> None:
        found = proposals("s16 f(s16 a) { return a; }\n")
        self.assertEqual(found[0].source, "u16 f(s16 a) { return a; }\n")
        self.assertEqual(found[0].description, f"{PRE.count(chr(10)) + 1}: s16 -> u16")
        self.assertEqual(found[1].source, "s16 f(u16 a) { return a; }\n")
        self.assertIn("s16 f(s8 a) { return a; }\n", texts("s16 f(s16 a) { return a; }\n"))
        self.assertTrue(all(mutation.kind == "types" for mutation in found))

    def test_cast_targets_and_pointer_targets_are_changed(self) -> None:
        source = "int f(s16 *p) { return (u8)p[0]; }\n"
        self.assertIn("int f(u16 *p) { return (u8)p[0]; }\n", texts(source))
        self.assertIn("int f(s16 *p) { return (s8)p[0]; }\n", texts(source))

    def test_word_spellings_keep_their_style(self) -> None:
        found = texts("unsigned char f(short a) { return a; }\n")
        self.assertIn("signed char f(short a) { return a; }\n", found)
        self.assertIn("unsigned char f(unsigned short a) { return a; }\n", found)
        self.assertIn("unsigned short f(short a) { return a; }\n", found)

    def test_pairs_of_changes_follow_the_single_ones(self) -> None:
        found = texts("s16 f(s16 a) { return a; }\n")
        self.assertIn("u16 f(u16 a) { return a; }\n", found)
        self.assertLess(found.index("u16 f(s16 a) { return a; }\n"), found.index("u16 f(u16 a) { return a; }\n"))

    def test_proposals_are_unique(self) -> None:
        found = texts("s16 f(s16 a, s16 b) { s16 c = a; return c + b; }\n")
        self.assertEqual(len(found), len(set(found)))

    def test_only_the_named_function_is_touched(self) -> None:
        source = "s16 g_x;\ns16 g(s16 a) { return a; }\ns32 f(s32 a) { return a; }\n"
        for text in texts(source):
            self.assertTrue(text.startswith("s16 g_x;\ns16 g(s16 a) { return a; }\n"), text)

    def test_comments_and_strings_are_not_spellings(self) -> None:
        source = '/* s16 */\nvoid f(void) { void *s = "s16 u8"; }\n'
        self.assertEqual(proposals(source), [])

    def test_no_integer_spelling_proposes_nothing(self) -> None:
        self.assertEqual(proposals("float f(float a) { return a; }\n"), [])

    def test_a_passed_deadline_proposes_nothing(self) -> None:
        self.assertEqual(proposals("s16 f(s16 a) { return a; }\n", seconds=-1.0), [])

    def test_missing_inputs_are_refused_by_name(self) -> None:
        for source, function, deadline, message in (
            ("", "f", 1e18, "source is required"),
            (PRE + "s16 f(void) { return 0; }\n", "", 1e18, "trial.function"),
            (PRE + "s16 f(void) { return 0; }\n", "f", float("inf"), "context.deadline"),
            (PRE + "s16 g(void) { return 0; }\n", "f", 1e18, "exactly one definition"),
            ("#define A 1\ns16 f(void) { return 0; }\n", "f", 1e18, "preprocessed"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(Held, message):
                list(method.propose(source, SimpleNamespace(function=function), SimpleNamespace(deadline=deadline)))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
