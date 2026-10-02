"""Header dependencies follow type positions in C declarations."""

import unittest
from pathlib import Path

from unbake.decomp.draft_context import ordered_headers, required_headers
from unbake.decomp.header_declarations import declaration_source, declarations
from unbake.layout.structs_parser import Parser
from unbake.project import makefile
from unbake.project.config import Held


class HeaderDeclarationsTests(unittest.TestCase):
    def test_logical_directives_keep_offsets_and_cannot_hide_the_next_typedef(self) -> None:
        source = (
            "#define WRITE(p) \\\n"
            "    { unsigned int ignored; } \\\n"
            "    while (0)\n"
            "#define COUNT 2\n"
            "typedef struct { unsigned int w[COUNT]; } Awords;\n"
            "typedef union { Awords words; long long align; } Acmd;\n"
        )
        clean = declaration_source(source)
        self.assertEqual(len(clean), len(source))
        self.assertEqual(
            [i for i, char in enumerate(clean) if char == "\n"], [i for i, char in enumerate(source) if char == "\n"]
        )
        self.assertEqual(declarations(source).typedefs, {"Awords", "Acmd"})
        records = Parser(source).parse()
        self.assertEqual([(record.name, record.size) for record in records], [("Awords", 8), ("Acmd", 8)])
        self.assertEqual(source[records[0].start : records[0].end], "struct { unsigned int w[COUNT]; }")

    def test_declaration_line_splices_still_join_identifier_tokens(self) -> None:
        source = "typedef int Wo\\\nrd; extern Wo\\\nrd value;"
        parsed = declarations(source)
        self.assertEqual(parsed.typedefs, {"Word"})
        self.assertEqual(parsed.uses, {"Word"})

    def test_comment_spelling_inside_literals_is_preserved(self) -> None:
        source = 'static char *url = "http://example"; /* hidden */\n'
        self.assertIn('"http://example";', declaration_source(source))
        self.assertNotIn("hidden", declaration_source(source))
        self.assertEqual(len(source), len(declaration_source(source)))

    def test_original_sdk_callbacks_have_only_their_four_typedef_names(self) -> None:
        source = (makefile.TEMPLATES / "audio_callbacks.h").read_text()
        parsed = declarations(source)
        self.assertEqual(parsed.typedefs, {"ALCmdHandler", "ALDMAproc", "ALVoiceHandler", "ALSetParam"})
        self.assertEqual(parsed.uses, {"Acmd"})
        callbacks, commands = Path("audio_callbacks.h"), Path("acmd.h")
        contents = {callbacks: source, commands: (makefile.TEMPLATES / "acmd.h").read_text()}
        self.assertEqual(ordered_headers(contents), [commands, callbacks])

    def test_nested_function_pointers_arrays_and_multiple_aliases(self) -> None:
        parsed = declarations(
            "typedef Result *(*const Handler)(Input, int, void (*)(Payload *, int), Input (*const)[4]), "
            "(*Other)(Input named);\n"
            "typedef Handler Table[4], *TablePointer;\n"
        )
        self.assertEqual(parsed.typedefs, {"Handler", "Other", "Table", "TablePointer"})
        self.assertEqual(parsed.uses, {"Result", "Input", "Payload", "Handler"})

    def test_value_names_tags_macros_and_extents_cannot_create_type_edges(self) -> None:
        first, second = Path("first.h"), Path("second.h")
        contents = {
            first: "typedef int Count; extern void call(int Other); struct Other { int Other; };",
            second: "#define Count(x) (x)\\\n + 1\ntypedef int Other; extern int Count[Other];",
        }
        self.assertEqual(ordered_headers(contents), [first, second])

    def test_real_types_in_unnamed_callbacks_create_transitive_dependencies(self) -> None:
        callback, payload, base = Path("callback.h"), Path("payload.h"), Path("base.h")
        contents = {
            callback: "typedef void (*Callback)(Payload, void (*)(Base));",
            payload: "typedef Base Payload;",
            base: "typedef int Base;",
        }
        self.assertEqual(ordered_headers(contents), [base, payload, callback])
        self.assertEqual(required_headers(contents, "Callback handler;"), set(contents))

    def test_complete_tag_fields_order_providers_without_pointer_cycles(self) -> None:
        consumer, provider = Path("consumer.h"), Path("provider.h")
        contents = {
            consumer: "struct Holder { struct Value values[2]; struct Value *pointer; };",
            provider: "struct Value { int number; struct Holder *owner; };",
        }
        self.assertEqual(ordered_headers(contents), [provider, consumer])
        records = Parser("\n".join(contents[path] for path in ordered_headers(contents))).parse()
        self.assertEqual([(record.name, record.size) for record in records], [("Value", 8), ("Holder", 20)])
        self.assertEqual(declarations(contents[consumer]).complete_uses, {"Value"})
        self.assertEqual(declarations(contents[provider]).complete_uses, set())

    def test_real_typedef_dependency_cycle_still_refuses(self) -> None:
        with self.assertRaisesRegex(Held, "cyclic shared type context: a.h -> b.h -> a.h"):
            ordered_headers({Path("a.h"): "typedef B A;", Path("b.h"): "typedef A B;"})

    def test_aggregates_forward_tags_enums_and_unnamed_bitfields(self) -> None:
        parsed = declarations(
            "struct Forward; typedef struct Tag { Field value; int : 3; "
            "void (*notify)(Payload); } TagAlias, *TagPointer;"
            "typedef enum Choice { FIRST, SECOND } ChoiceAlias;"
        )
        self.assertEqual(parsed.typedefs, {"TagAlias", "TagPointer", "ChoiceAlias"})
        self.assertEqual(parsed.uses, {"Field", "Payload"})
        self.assertEqual(parsed.exports, {"Tag", "Choice"})
