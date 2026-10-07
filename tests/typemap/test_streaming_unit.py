"""Body blanking retains declaration meaning, strings, directives and exact line positions."""

import re
import unittest

from unbake.config import Held
from unbake.typemap import declarations


class StreamingUnitTests(unittest.TestCase):
    def test_signatures_aggregates_initializers_and_logical_markers_survive(self):
        source = (
            "struct S { int x; };\n"
            "struct S value = { 3 };\n"
            'char *text = "{not a body}";\n'
            "int f(int x) {\n"
            "#define BLOCK \\\n"
            "  }\n"
            "/* } */ if(x) { return x; }\n"
            "return 0;\n"
            "}\n"
            "int g(void) { return 1; }\n"
        )
        result = declarations._unit_bodies_blanked(source)
        self.assertEqual(len(result), len(source))
        self.assertEqual([m.start() for m in re.finditer("\n", result)], [m.start() for m in re.finditer("\n", source)])
        self.assertIn("struct S value = { 3 };", result)
        self.assertIn('"{not a body}"', result)
        self.assertIn("int f(int x) {", result)
        self.assertIn("int g(void) {", result)
        self.assertNotIn("return", result)
        self.assertEqual(declarations._unit_bodies_blanked(result), result)

    def test_unclosed_body_is_a_named_refusal(self):
        with self.assertRaisesRegex(Held, "unclosed function body"):
            declarations._unit_bodies_blanked('int f(void) { "}";')

    def test_multiline_body_refusal_retains_cpp_location_and_function_context(self):
        source = '# 12 "src/broken.c"\nint broken\n(void)\n{\n'
        with self.assertRaises(Held) as raised:
            declarations._unit_bodies_blanked(source)
        self.assertIn("src/broken.c:14", raised.exception.reason)
        self.assertIn("broken", raised.exception.reason)
