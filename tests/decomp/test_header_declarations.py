"""Header dependencies follow type positions in C declarations."""

import unittest
from pathlib import Path

from unbake.decomp.draft_context import ordered_headers, required_headers
from unbake.decomp.header_declarations import declarations
from unbake.project import makefile
from unbake.project.config import Held


class HeaderDeclarationsTests(unittest.TestCase):
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
