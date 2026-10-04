"""Mocked compiler token provenance and source-preserving layout rewrites."""

import re
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.project_fixture import ProjectCase
from unbake.cdecl import LayoutParser
from unbake.config import Held
from unbake.layout.structs import layouts
from unbake.fold import rewrite_view, type_rewrite


def dump(tokens):
    return "".join(
        f"{{P:{file};F:<NULL>;L:{line};C:{col};S:0;M:0x1;E:0,LOC:1}}{token}\n" for token, file, line, col in tokens
    )


def expanded(source, filename="/source.c", extras=None):
    """Mock a compiler spelling stream, optionally replacing an invocation."""
    tokens = [
        ("extern", "<stdin>", 1, 1),
        ("int", "<stdin>", 1, 8),
        (rewrite_view._BOUNDARY, "<stdin>", 1, 12),
        (";", "<stdin>", 1, 40),
    ]
    for line, text in enumerate(source.splitlines(), 1):
        if text.lstrip().startswith("#"):
            continue
        for match in re.finditer(rewrite_view._TOKEN, text):
            if extras and match[0] in extras:
                tokens.extend(extras[match[0]])
            else:
                tokens.append((match[0], filename, line, match.start() + 1))
    return rewrite_view.decode(dump(tokens), source, filename)


class RewriteViewTests(unittest.TestCase):
    def test_callback_alias_collision_rewrites_macro_spelling_and_preserves_values(self):
        source = (
            "typedef int (*Callback)(int);\n"
            "#define LOCAL_CALLBACK Callback\n"
            "int alpha(void) {LOCAL_CALLBACK local; int Callback = 1; return Callback;}\n"
        )
        view = expanded(source, extras={"LOCAL_CALLBACK": [("Callback", "/source.c", 2, 24)]})
        parser = LayoutParser(source)
        parser.parse()
        planned = type_rewrite.edits(
            parser,
            "typedef float (*Callback)(float);",
            {},
            typedef_renames={"Callback": "Callback_alpha"},
            preprocess=lambda: view,
            source_path=Path("/source.c"),
            source_text=source,
        )
        for (start, end), target in sorted(planned.items(), reverse=True):
            source = source[:start] + target + source[end:]
        self.assertIn("(*Callback_alpha)(int)", source)
        self.assertIn("#define LOCAL_CALLBACK Callback_alpha", source)
        self.assertIn("LOCAL_CALLBACK local; int Callback = 1; return Callback;", source)

    def test_macro_arguments_repeated_expansions_and_header_tokens_have_exact_origins(self):
        source = "\tint f(Old *p) {return TWICE(p->old);}\n"
        tokens = [
            ("extern", "<stdin>", 1, 1),
            ("int", "<stdin>", 1, 8),
            (rewrite_view._BOUNDARY, "<stdin>", 1, 12),
            (";", "<stdin>", 1, 40),
        ]
        at = source.index("old")
        tokens += [
            ("(", "/header.h", 8, 4),
            ("old", "/source.c", 1, at + 1),
            ("+", "/header.h", 8, 10),
            ("old", "/source.c", 1, at + 1),
            (")", "/header.h", 8, 12),
        ]
        view = rewrite_view.decode(dump(tokens), source, "/source.c")
        self.assertEqual(view.text, "(\nold\n+\nold\n)\n")
        self.assertEqual(view.origins, (None, at, None, at, None))

    def test_pasted_and_builtin_tokens_never_guess_source_spelling(self):
        source = "CAT(a,b) __LINE__"
        tokens = [
            (rewrite_view._BOUNDARY, "<stdin>", 1, 1),
            (";", "<stdin>", 1, 2),
            ("ab", "/source.c", 1, 1),
            ("42", "/source.c", 1, 10),
        ]
        view = rewrite_view.decode(dump(tokens), source, "/source.c")
        self.assertEqual(view.origins, (None, None))

    def test_map_looking_literals_exponents_and_operators_stay_whole_tokens(self):
        literal = '"{P:fake;F:x;L:99;C:1;S:0;M:0x1;E:0}"'
        source = f"char *s = {literal}; float f = 3.4e-5f; int n = a >> 2;"
        view = expanded(source)
        self.assertIn(literal, view.text.splitlines())
        self.assertIn("3.4e-5f", view.text.splitlines())
        self.assertIn(">>", view.text.splitlines())

    def test_missing_compiler_provenance_is_an_explicit_source_diagnostic(self):
        with self.assertRaisesRegex(Held, r"/source.c:1:.*-fdebug-cpp"):
            rewrite_view.decode("int f(void) {return 0;}", "", "/source.c")

    def rewrite(self, source, view, layout_source=None):
        parser = LayoutParser(source if layout_source is None else layout_source)
        parser.parse()
        context = "typedef struct Canon {int value;} Canon;"
        replacements = type_rewrite.edits(
            parser,
            context,
            {"Old": ("Canon", layouts(context)[0])},
            preprocess=Mock(return_value=view),
            source_path=Path("/source.c"),
            source_text=source,
        )
        for (start, end), target in sorted(replacements.items(), reverse=True):
            source = source[:start] + target + source[end:]
        return source

    def test_macro_supplied_semicolon_rewrites_argument_members_without_expanding_body(self):
        source = "typedef struct Old {int old;} Old;\nint f(Old *p, int x) {if (x) STMT(p->old) else return p->old;}\n"
        # STMT's macro body contributes syntax; the argument keeps its spelling.
        tokens = [
            ("extern", "<stdin>", 1, 1),
            ("int", "<stdin>", 1, 8),
            (rewrite_view._BOUNDARY, "<stdin>", 1, 12),
            (";", "<stdin>", 1, 40),
        ]
        for line, text in enumerate(source.splitlines(), 1):
            for match in re.finditer(rewrite_view._TOKEN, text):
                if match[0] == "STMT":
                    tokens.append(("return", "/header.h", 2, 10))
                else:
                    tokens.append((match[0], "/source.c", line, match.start() + 1))
                    if line == 2 and match[0] == ")" and match.start() == text.index(") else"):
                        tokens.append((";", "/header.h", 2, 20))
        result = self.rewrite(source, rewrite_view.decode(dump(tokens), source, "/source.c"))
        self.assertEqual(
            result,
            source.replace("struct Old", "struct Canon")
            .replace("} Old", "} Canon")
            .replace("f(Old", "f(Canon")
            .replace("int old;", "int value;")
            .replace("p->old", "p->value"),
        )
        self.assertIn("STMT(p->value) else", result)

    def test_source_macro_replacement_type_is_editable_and_unrelated_values_stay(self):
        source = (
            "#define TYPE Old\ntypedef struct Old {int old;} Old;\nint f(TYPE *p) {int Old=1; return p->old+Old;}\n"
        )
        view = expanded(source, extras={"TYPE": [("Old", "/source.c", 1, 14)]})
        selected = source.replace("#define TYPE Old", " " * len("#define TYPE Old"))
        result = self.rewrite(source, view, layout_source=selected)
        self.assertIn("#define TYPE Canon", result)
        self.assertIn("int Old=1; return p->value+Old;", result)
        self.assertIn("f(TYPE *p)", result)

    def test_macro_definition_shared_by_type_and_value_cannot_modify_unchanged_use(self):
        source = "#define TOKEN Old\ntypedef struct Old {int old;} Old;\nint f(TOKEN *p) {int Old=1; return TOKEN;}\n"
        view = expanded(source, extras={"TOKEN": [("Old", "/source.c", 1, 15)]})
        with self.assertRaisesRegex(Held, "macro spelling also supplies an unchanged"):
            self.rewrite(source, view)

    def test_uneditable_header_macro_type_is_held_without_guessing_an_edit(self):
        source = "typedef struct Old {int old;} Old;\nint f(TYPE *p) {return p->old;}\n"
        view = expanded(source, extras={"TYPE": [("Old", "/header.h", 2, 14)]})
        with self.assertRaisesRegex(Held, "no editable source provenance"):
            self.rewrite(source, view)

    def test_every_parse_message_reports_source_coordinates_without_header_prefix(self):
        source = "typedef struct Old {int old;} Old;\nint f(Old *p) {return p->old;}\n"
        parser = LayoutParser(source)
        parser.parse()
        prefix = "\n" * 8200 + "typedef struct Canon {int value;} Canon;\n"
        record = layouts("typedef struct Canon {int value;} Canon;")[0]
        messages = [
            "before: else",
            "Invalid expression",
            "Invalid declaration",
            "Invalid specifier list",
            "Missing type in declaration",
            "before: {",
        ]
        for message in messages:
            for location in (f":{prefix.rstrip().count(chr(10)) + 3}:9: ", ": "):
                with (
                    self.subTest(message=message, location=location),
                    patch.object(
                        type_rewrite, "_parse", side_effect=type_rewrite.c_parser.ParseError(location + message)
                    ),
                    self.assertRaises(Held) as caught,
                ):
                    type_rewrite.edits(parser, prefix, {"Old": ("Canon", record)}, source_path=Path("/source.c"))
                expected = "/source.c:2:9:" if location != ": " else "/source.c:1:1:"
                self.assertIn(expected, caught.exception.reason)
                self.assertNotIn("820", caught.exception.reason)

    def test_expanded_parse_error_maps_to_argument_source_and_subtracts_evidence_prefix(self):
        source = "\n" * 7 + "typedef struct Old {int old;} Old;\nint f(Old *p) {return p->old;}\n"
        parser = LayoutParser(source)
        parser.parse()
        view = expanded(source)
        index = next(i for i, loc in enumerate(view.locations) if loc.line == 9)
        prefix = "\n" * 8200 + "typedef struct Canon {int value;} Canon;\n"
        normalized = prefix.rstrip() + "\n"
        with (
            patch.object(
                type_rewrite,
                "_parse",
                side_effect=type_rewrite.c_parser.ParseError(
                    f":{normalized.count(chr(10)) + index + 1}:1: before: else"
                ),
            ),
            self.assertRaises(Held) as caught,
        ):
            type_rewrite.edits(
                parser,
                prefix,
                {"Old": ("Canon", layouts(normalized)[0])},
                preprocess=Mock(return_value=view),
                source_path=Path("/source.c"),
                source_line_offset=7,
            )
        self.assertIn("/source.c:2:1: before: else", caught.exception.reason)

    def test_unlocated_parser_messages_gain_a_token_location(self):
        for view in ("typedef Missing T;", "int f(void) {return (int);}"):
            with self.subTest(view=view), self.assertRaisesRegex(type_rewrite.c_parser.ParseError, r":\d+:\d+:"):
                type_rewrite._parse("", view)


class PrepareTests(ProjectCase):
    def test_injected_preprocessor_uses_compiler_and_version_macros_and_private_includes(self):
        source = "int alpha(void) {return 1;}"
        path = Path("/authored/alpha.c")
        output = dump([(rewrite_view._BOUNDARY, "<stdin>", 1, 1), (";", "<stdin>", 1, 2), ("int", str(path), 1, 1)])
        run = Mock(return_value=output)
        self.project = replace(
            self.project,
            version_map={**self.project.version_map, "us": replace(self.project.version("us"), macros=("VERSION_US",))},
        )
        view = rewrite_view.prepare(self.project, self.host, source, "us", path, preprocess=run)
        project, command, unit = run.call_args.args
        self.assertIs(project, self.project)
        for flag in ("-P", "-fdebug-cpp", "-ftrack-macro-expansion=2", "-ftabstop=1", "-DVERSION_US"):
            self.assertIn(flag, command)
        self.assertIn("-I" + str(self.project.include[0]), command)
        self.assertIn('#line 1 "/authored/alpha.c"\n' + source, unit)
        self.assertEqual(view.origins, (0,))


class HeaderOutputSkipTests(unittest.TestCase):
    def test_skipping_header_tokens_preserves_provenance_and_rejects_marker_like_literals(self):
        source = "int f(void) {return 1;}"
        filename = "/source.c"
        marker = (rewrite_view._BOUNDARY, "<stdin>", 30, 12)
        suffix = dump([marker, (";", "<stdin>", 30, 40), ("int", filename, 1, 1)])
        false_marker = dump([marker]).rstrip("\n")
        prefixes = (
            dump([("Header", "/header.h", 1, 1)]),
            dump([('"' + false_marker + '"', "/header.h", 1, 1)]),
            dump([('"prefix ' + false_marker + ' suffix"', "/header.h", 1, 1)]),
            dump([(rewrite_view._BOUNDARY, "/header.h", 30, 12)]),
            dump([(rewrite_view._BOUNDARY, "<stdin>", 29, 12)]),
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                output = prefix + suffix
                trimmed = rewrite_view._source_output(output, 30)
                self.assertEqual(trimmed, suffix)
                # The expected source suffix is identical, including its origins.
                self.assertEqual(
                    rewrite_view.decode(trimmed, source, filename), rewrite_view.decode(suffix, source, filename)
                )
                if rewrite_view._BOUNDARY not in prefix:
                    self.assertEqual(
                        rewrite_view.decode(trimmed, source, filename), rewrite_view.decode(output, source, filename)
                    )

    def test_unsupported_or_missing_stdin_mapping_keeps_the_original_decoder_path(self):
        for output in (
            "plain compiler output",
            dump([(rewrite_view._BOUNDARY, "<stdin>", 1, 1)]),
            dump([(rewrite_view._BOUNDARY, "/header.h", 30, 12)]),
        ):
            with self.subTest(output=output):
                self.assertEqual(rewrite_view._source_output(output, 30), output)
