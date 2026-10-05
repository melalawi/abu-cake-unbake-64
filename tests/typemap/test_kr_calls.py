"""A (void) prototype never reaches a header when mapped callers pass the function arguments (K&R calls)."""

import unittest

from unbake.layout import redeclarations
from unbake.typemap.database import unprototyped_calls, without_void


def record(prototype: str | None, caller_arguments: list[str]) -> dict:
    return {"prototype": prototype, "abi": {"caller_arguments": caller_arguments}}


class KRCallTests(unittest.TestCase):
    def test_which_functions_lose_their_void_list(self) -> None:
        cases = [
            # The RW case: proven `void f(void)`, a caller declares `f()` and passes one argument.
            ("void func_80293DE8_de(void);", ["r4"], True),
            ("extern int f(void);", ["r4", "stack16"], True),
            # Near misses: no caller argument, a real parameter list, an empty list already, no prototype.
            ("void f(void);", [], False),
            ("void f(int a);", ["r4"], False),
            ("void f();", ["r4"], False),
            ("void f(void (*callback)(void), int a);", ["r4", "r5"], False),
            (None, ["r4"], False),
        ]
        # A function the map never saw has no ABI record at all.
        self.assertEqual(unprototyped_calls({"f": {"prototype": "void f(void);", "abi": None}}), set())
        self.assertEqual(unprototyped_calls({"f": {"prototype": "void f(void);"}}), set())
        for prototype, arguments, expected in cases:
            with self.subTest(prototype=prototype, arguments=arguments):
                self.assertEqual(unprototyped_calls({"f": record(prototype, arguments)}) == {"f"}, expected)

    def test_rendered_and_carried_spellings(self) -> None:
        carried = (
            "/* unbake published declaration: published_x */\n"
            "extern void func_80293DE8_de(void);\n"
            "extern void func_80293B28_de(void *);\n"
            "extern void other(void);\n"
        )
        rewritten = without_void(carried, {"func_80293DE8_de"})
        self.assertIn("extern void func_80293DE8_de();", rewritten)
        # Only the named function changes; a similar name and other functions keep (void).
        self.assertIn("extern void other(void);", rewritten)
        self.assertEqual(
            without_void("void func_80293DE8_de_x(void);", {"func_80293DE8_de"}), "void func_80293DE8_de_x(void);"
        )
        self.assertEqual(without_void(carried, set()), carried)

    def test_the_callers_local_declaration_matches_the_header_after_the_rewrite(self) -> None:
        # Layout apply strips a local declaration equivalent to the header's: with `f()` the caller's call compiles.
        header = without_void("extern void func_80293DE8_de(void);", {"func_80293DE8_de"})
        self.assertTrue(redeclarations.equivalent("extern void func_80293DE8_de();", header, {}))
