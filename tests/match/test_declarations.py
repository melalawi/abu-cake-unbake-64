"""Shared declaration preflight and final source regressions."""

from dataclasses import asdict
from itertools import pairwise

from tests.match.support import MatchFixture
from unbake.decomp import needs
from unbake.layout.structs import layouts
from unbake.layout.structs_parser import Parser
from unbake.match import declarations


class DeclarationTests(MatchFixture):
    def setUp(self):
        super().setUp()
        from tests.preprocessor import output
        from tests.process_fakes import boundary
        from unbake.layout import structs
        from unbake.typemap import declarations

        for mock in (boundary(structs, output), boundary(declarations, output)):
            mock.start()
            self.addCleanup(mock.stop)

    def test_existing_c_row_with_rodata_can_be_resubmitted(self) -> None:
        source = self.src / "alpha.c"
        source.write_text("int alpha(void) {return 1;}\n")
        for version in self.versions:
            path = self.project.version(version).split
            text = path.read_text().replace("asm, text/alpha]", "c, alpha]")
            text = text.replace("c, alpha]\n", "c, alpha]\n      - [0x1008, .rodata, alpha]\n")
            path.write_text(text)
        edits = declarations.match_edits(self.project, "alpha", source.read_text(), self.versions)
        self.assertEqual([edit.path for edit in edits], [source])

    def test_imported_value_and_callback_types_are_included_in_folded_header(self) -> None:
        header = self.root / "include" / "types.h"
        header.write_text(
            "#ifndef TYPES_H\n#define TYPES_H\n"
            "typedef struct Vec3f {float x,y,z;} Vec3f;\n"
            "typedef int (*Callback)(void *);\n#endif\n"
            "typedef char imported_size_check[(sizeof(Vec3f) == 12) ? 1 : -1];\n"
        )
        text = '#include "types.h"\nstruct Holder {Vec3f position;Callback handler;};\n'
        text += "int alpha(struct Holder *p) {return p->handler(p);}\n"
        edits = declarations.folded_edits(self.project, self.policy, "alpha", text, self.versions)
        destination = self.root / "include" / "shared" / "alpha.h"
        generated = next(edit.after for edit in edits if edit.path == destination)
        self.assertIn('#include "types.h"', generated)
        for edit in edits:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)
        from pycparser import c_parser

        from tests.preprocessor import expand

        expanded = expand((self.src / "alpha.c").read_text(), (self.root / "include",))
        c_parser.CParser().parse(expanded)
        holder = next(record for record in layouts(expanded) if record.name == "Holder")
        self.assertEqual((holder.size, holder.alignment), (16, 4))
        self.assertEqual([(field.offset, field.size) for field in holder.fields], [(0, 12), (12, 4)])

    def test_local_scalar_and_callback_aliases_move_with_promoted_fields(self) -> None:
        text = (
            "typedef int Count, Spare;\ntypedef Count Amount;\n"
            "typedef void (*Callback)(void *, Amount);\n"
            "struct Holder { Amount count; Callback handler; };\n"
            "int alpha(struct Holder *p) { Spare value = p->count; return value; }\n"
        )
        edits = declarations.folded_edits(self.project, self.policy, "alpha", text, self.versions)
        destination = self.root / "include/shared/alpha.h"
        generated = next(edit.after for edit in edits if edit.path == destination)
        parsed = Parser(generated)
        record = parsed.parse()[0]
        self.assertEqual((record.size, record.alignment), (8, 4))
        self.assertEqual([(field.offset, field.size) for field in record.fields], [(0, 4), (4, 4)])
        source = next(edit.after for edit in edits if edit.path == self.src / "alpha.c")
        self.assertNotIn("typedef void (*Callback)", source)
        self.assertIn("typedef void (*Callback)", generated)
        for edit in edits:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)

    def test_opaque_pointer_alias_does_not_escape_into_shared_header(self) -> None:
        text = "typedef struct Opaque_s Opaque;\nstruct Holder {Opaque *pointer;struct Opaque_s *other;};\n"
        text += "int alpha(struct Holder *p) {return p->pointer != 0;}\n"
        edits = declarations.folded_edits(self.project, self.policy, "alpha", text, self.versions)
        destination = self.root / "include" / "shared" / "alpha.h"
        generated = next(edit.after for edit in edits if edit.path == destination)
        self.assertIn("struct Opaque_s *pointer;", generated)
        for edit in edits:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)

    def test_resolved_aggregate_aliases_are_emitted_after_tag_forwards(self) -> None:
        from pycparser import c_parser

        from tests.preprocessor import expand
        from unbake.layout.header_context import Headers

        for kind in ("struct", "union"):
            with self.subTest(kind=kind):
                # The shared name is occupied by a different layout, forcing an
                # owner name. Alias and its chain collapse to that same name.
                header = self.root / "include" / "occupied.h"
                header.write_text(f"typedef {kind} Local {{ float other; }} Local;\n")
                text = (
                    f"typedef {kind} Local Local;\n"
                    f"{kind} Local {{ int value; }};\n"
                    "typedef Local Alias;\n"
                    "typedef Alias Chained;\n"
                    "struct Holder { Chained *entry; };\n"
                    "int alpha(struct Holder *p) { return p->entry->value; }\n"
                )
                folded = declarations.fold_source(
                    self.project,
                    self.policy,
                    Headers.read(self.project),
                    "alpha",
                    text,
                    self.versions,
                    prove_headers=False,
                )
                generated = next(edit.after for edit in folded.headers if edit.path.name == "alpha.h")
                self.assertNotIn("typedef Local_alpha Local_alpha;", generated)
                self.assertIn(f"typedef {kind} Local_alpha Local_alpha;", generated)
                for edit in folded.headers:
                    edit.path.parent.mkdir(parents=True, exist_ok=True)
                    edit.path.write_text(edit.after)
                # A new source sees a valid shared context, just as the next
                # unrelated exact candidate in a submit batch does.
                expanded = expand(folded.source, self.project.include)
                c_parser.CParser().parse(expanded)
                declarations.fold_source(
                    self.project,
                    self.policy,
                    Headers.read(self.project),
                    "beta",
                    "typedef struct Other {int value;} Other;\nint beta(Other *p) {return p->value;}\n",
                    self.versions,
                    prove_headers=False,
                )
                for edit in folded.headers:
                    edit.path.unlink()

    def test_forward_typedef_spans_preserve_externs_and_function_body(self) -> None:
        for kind in ("struct", "union"):
            with self.subTest(kind=kind):
                forwards = [f"typedef {kind} {name} {name};" for name in ("First", "Second", "Third")]
                definitions = [f"{kind} {name} {{ int value; }};" for name in ("First", "Second", "Third")]
                extern = "extern Second *global;"
                body = "int alpha(First *arg) { return arg->value + global->value; }"
                text = "\n".join([*forwards, definitions[0], extern, *definitions[1:], body]) + "\n"
                parser = Parser(text)
                records = parser.parse()
                self.assertEqual(
                    [text[item.start : item.end] for item in parser.declarations],
                    [
                        *forwards,
                        *definitions,
                    ],
                )
                self.assertEqual(
                    [text[item.start : item.end] for item in records], [definition[:-1] for definition in definitions]
                )
                self.assertTrue(all(first.end <= second.start for first, second in pairwise(records)))

    def pending(self, text: str) -> list[needs.Need]:
        record = layouts(text)[0]
        return [
            needs.LayoutNeed(
                version,
                record.name,
                [asdict(member) for member in record.fields],
                str(self.sources / "alpha.c"),
                dict(kind=record.kind, size=record.size, alignment=record.alignment, aliases=list(record.aliases)),
            )
            for version in self.versions
        ]
