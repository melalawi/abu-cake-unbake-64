"""Authored declaration selection, shared folding and feedback boundaries."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from unbake.config import Held
from unbake.layout import headers as mapped_headers
from unbake.layout import map as ownership
from unbake.typemap import declaration_evidence as evidence


class SelectionTests(TestCase):
    def test_selection_table_and_dependency_closure(self):
        old = Path("/old/include")
        root = Path("/live/include")
        project = SimpleNamespace(declaration_evidence=(old,), include=(root,))
        rows = evidence.units(
            {
                old / "shared/session.h": """#ifndef OLD_SESSION_H
#define OLD_SESSION_H
typedef struct Vec3f {float x; float y; float z;} Vec3f;
typedef struct View {Vec3f position;} View;
extern View global;
extern int call(View *p);
#define VALUE 3
#define ACCESS(p) ((View *)(p))->position.x
enum Mode {READY=1, WAITING=2};
#endif
""",
                old / "unrelated.h": (
                    "typedef int Unrelated;\nstatic int implementation(void) {return 1;}\nint initialized = 3;\n"
                ),
            }
        )
        empty = SimpleNamespace(texts={})
        cases = (
            ("type closure", "void alpha(View *p) {p->position.x=0;}", empty, {"View", "Vec3f"}),
            ("data closure", "int alpha(void) {return global.position.x;}", empty, {"global", "View", "Vec3f"}),
            ("prototype closure", "int alpha(void *p) {return call(p);}", empty, {"call", "View", "Vec3f"}),
            (
                "macro closure",
                "int alpha(void *p) {return ACCESS(p)+VALUE;}",
                empty,
                {"ACCESS", "VALUE", "View", "Vec3f"},
            ),
            ("enum", "int alpha(void) {return READY;}", empty, {"READY", "WAITING"}),
            ("local ownership", "typedef int View; int alpha(View p) {return p;}", empty, set()),
            ("comments strings", 'char *alpha(void) {/* View */ return "global VALUE";}', empty, set()),
            (
                "installed",
                "int alpha(void) {return VALUE;}",
                SimpleNamespace(texts={root / "sdk.h": "#define VALUE 3\n"}),
                set(),
            ),
            ("unknown", "void alpha(Missing *p) {}", empty, set()),
            ("condition", "#if VALUE\nint alpha(void) {return 1;}\n#endif", empty, {"VALUE"}),
        )
        with patch.object(evidence, "catalogue", return_value=rows):
            for label, text, headers, expected in cases:
                with self.subTest(label=label):
                    selected = evidence.select(project, headers, text, "alpha")
                    self.assertEqual(set().union(*(unit.names for unit in selected)), expected)
        self.assertFalse(any("initialized" in unit.names or "implementation" in unit.names for unit in rows))
        self.assertEqual(evidence.units({Path("/old.h"): "typedef int M2C_UNK;"}), ())

    def test_full_layout_retains_its_separate_authored_typedef(self):
        project = SimpleNamespace(include=(Path("/live"),), declaration_evidence=())
        for kind in ("struct", "union"):
            alias = f"typedef {kind} Record Record;\n"
            definition = f"{kind} Record {{int value;}};\n"
            rows = evidence.units({Path("/old.h"): alias + definition})
            for use in ("Record *alpha(Record *p) {return p;}", f"{kind} Record *alpha(void);"):
                with self.subTest(kind=kind, use=use), patch.object(evidence, "catalogue", return_value=rows):
                    selected = evidence.select(project, SimpleNamespace(texts={}), use, "alpha")
                    self.assertEqual({unit.text for unit in selected}, {alias, definition})

    def test_ambiguous_declarations_hold_and_duplicate_evidence_reuses(self):
        root = Path("/include")
        project = SimpleNamespace(include=(root,), declaration_evidence=())
        for second, held in (("typedef int T;", False), ("typedef float T;", True)):
            rows = evidence.units({Path("/a.h"): "typedef int T;", Path("/b.h"): second})
            with self.subTest(second=second), patch.object(evidence, "catalogue", return_value=rows):
                if held:
                    with self.assertRaisesRegex(Held, "ambiguous authored declarations"):
                        evidence.select(project, SimpleNamespace(texts={}), "T alpha(T x) {return x;}", "alpha")
                else:
                    self.assertEqual(
                        len(evidence.select(project, SimpleNamespace(texts={}), "T alpha(T x) {return x;}", "alpha")), 1
                    )

    def test_symbol_inventory_table(self):
        project = SimpleNamespace(names_from="us", version=lambda v: SimpleNamespace(symbols=Path("/" + v)))
        cases = (
            ("known data", "extern int global;", {"global": (0x80001000, 0, None)}, None),
            ("known function", "int call(void);", {"call": (0x80001000, 0, None)}, None),
            ("missing data", "extern int global;", {}, "absent live symbol/address inventory"),
            (
                "address conflict",
                "extern int D_80001000;",
                {"D_80001000": (0x80002000, 0, None)},
                "live address disagrees",
            ),
            ("matching address", "extern int D_80001000;", {"D_80001000": (0x80001000, 0, None)}, None),
            ("macro", "#define VALUE 3\n", {}, None),
            (
                "unknown pointer macro",
                "#define memory (*(int *)0x80001000)\n",
                {},
                "absent live symbol/address inventory",
            ),
            ("known pointer macro", "#define memory (*(int *)0x80001000)\n", {"memory": (0x80001000, 0, None)}, None),
            (
                "wrong pointer macro",
                "#define memory (*(int *)0x80002000)\n",
                {"memory": (0x80001000, 0, None)},
                "address-valued macro disagrees",
            ),
        )
        for label, text, symbols, reason in cases:
            with self.subTest(label=label), patch.object(evidence.inventory, "symbols", return_value=("", symbols)):
                selected = evidence.units({Path("/old.h"): text})
                if reason:
                    with self.assertRaisesRegex(Held, reason):
                        evidence.validate_symbols(project, selected, ("us",))
                else:
                    evidence.validate_symbols(project, selected, ("us",))

    def test_address_identity_can_belong_to_another_cartridge_version(self):
        project = SimpleNamespace(
            versions=("us", "eu"), names_from="eu", version=lambda v: SimpleNamespace(symbols=Path("/" + v))
        )
        selected = evidence.units({Path("/old.h"): "extern int D_80001000;"})

        def inventory(path):
            return "", {"D_80001000": (0x80001000 if path.name == "us" else 0x80202000, 0, None)}

        with patch.object(evidence.inventory, "symbols", side_effect=inventory):
            evidence.validate_symbols(project, selected, ("eu",))

    def test_no_evidence_preserves_source_and_performs_no_inventory_read(self):
        project = SimpleNamespace(declaration_evidence=(), include=(Path("/include"),))
        with patch.object(evidence.inventory, "symbols") as symbols:
            self.assertEqual(
                evidence.inject(project, SimpleNamespace(texts={}), "void alpha(void) {}", "alpha", ("us",)),
                ("void alpha(void) {}", 0),
            )
        symbols.assert_not_called()


class SplitEvidenceTests(TestCase):
    def test_evidence_macro_body_closure_enum_constants_and_plain_prototypes(self):
        root = Path("/include")
        marker = "/* unbake declaration evidence: evidence_1234abcd */\n"
        contents = {
            root / "shared/.evidence_type.h": marker + "typedef struct Record {int value;} Record;",
            root / "shared/.evidence_macro.h": marker + "#define ACCESS(p) (((Record *)(p))->value + COUNT)\n",
            root / "shared/.evidence_count.h": marker + "#define COUNT 3\n",
            root / "shared/.evidence_enum.h": marker + "enum Mode {READY=1};\n",
            root / "shared/.evidence_call.h": marker + "int call(Record *);\n",
        }
        layout = mapped_headers.Layout(contents, contents, root, ownership=ownership.Map(2, ()), sources={})
        macro = layout.homes[root / "shared/.evidence_macro.h"]
        layout.headers[macro].decode()
        for dependency in ("type", "count"):
            home = layout.homes[root / ("shared/.evidence_" + dependency + ".h")]
            self.assertEqual(home, macro)
        for name, kind in (("READY", "enum"), ("call", "call"), ("ACCESS", "macro")):
            with self.subTest(name=name):
                required = layout.required(f"int alpha(void) {{return {name}(0);}}")
                self.assertIn(layout.homes[root / ("shared/.evidence_" + kind + ".h")], required)
