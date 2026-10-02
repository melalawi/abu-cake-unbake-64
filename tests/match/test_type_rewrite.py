"""Resolved source edits follow C type and member namespaces."""

import unittest

from unbake.layout.structs import layouts
from unbake.layout.structs_identity import resolve
from unbake.layout.structs_parser import Parser
from unbake.match import type_rewrite


class TypeRewriteTests(unittest.TestCase):
    def rewrite(self, source, context):
        parser = Parser(source)
        records = parser.parse()
        resolution = resolve(records, layouts(context))
        for (start, end), target in sorted(type_rewrite.edits(parser, context, resolution).items(), reverse=True):
            source = source[:start] + target + source[end:]
        return source

    def test_member_names_and_shadowed_values_follow_their_own_types(self):
        context = "typedef struct Canon { short value; struct Canon *next; } Canon;"
        source = (
            "/* Owner stays in this comment. */\n"
            "typedef struct Owner {short Owner; struct Owner *tail;} Owner;\n"
            "typedef struct Other {float Owner;} Other;\n"
            "extern Owner *global;\n"
            "int alpha(Owner *p, Other *other) {\n"
            "  { int Owner = 2; p->Owner += Owner; }\n"
            "  return p->tail->Owner + global->Owner + other->Owner;\n}\n"
        )
        result = self.rewrite(source, context)
        self.assertIn("short value; struct Canon *next;", result)
        self.assertIn("int Owner = 2; p->value += Owner;", result)
        self.assertIn("p->next->value + global->value + other->Owner", result)
        self.assertIn("extern Canon *global;", result)
        self.assertIn("/* Owner stays in this comment. */", result)

    def test_casts_array_accesses_callbacks_and_nested_fields(self):
        context = "typedef struct Canon { int value; } Canon;\ntypedef struct Parent { Canon values[2]; } Parent;\n"
        source = (
            "typedef struct Old { int old_value; } Old;\n"
            "typedef struct Container { Old items[2]; } Container;\n"
            "extern Old *get(void);\n"
            "int alpha(Container *p, void *raw) {\n"
            " return p->items[1].old_value + ((Old *)raw)->old_value + get()->old_value;\n}\n"
        )
        result = self.rewrite(source, context)
        self.assertIn("p->values[1].value + ((Canon *)raw)->value + get()->value", result)
        self.assertIn("Canon values[2]", result)

    def test_string_identifiers_and_values_are_not_type_tokens(self):
        context = "typedef struct Canon { int value; } Canon;"
        source = (
            "typedef struct Old { int value; } Old;\n"
            'const char *name = "Old // still a string /* here */";\n'
            "int alpha(void) { int Old = 1; return Old; }\n"
        )
        result = self.rewrite(source, context)
        self.assertIn('"Old // still a string /* here */"', result)
        self.assertIn("int Old = 1; return Old;", result)
