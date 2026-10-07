"""Per-source facts keys follow exactly what the facts depend on."""

from types import SimpleNamespace
from typing import ClassVar

from tests.kit import TempCase, host_values
from unbake.config import Host
from unbake.typemap import facts


class SourceKeyTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        include = self.root / "include"
        include.mkdir()
        (include / "used.h").write_text('#include "nested.h"\ntypedef int Used;\n')
        (include / "nested.h").write_text("typedef int Nested;\n")
        (include / "unrelated.h").write_text("typedef int Unrelated;\n")
        self.source = self.root / "src" / "alpha.c"
        self.source.parent.mkdir()
        self.source.write_text('#include "used.h"\nint alpha(void) { return 1; }\n')
        versions = {name: SimpleNamespace(macros=(f"VERSION_{name.upper()}",)) for name in ("us", "eu")}
        self.project = SimpleNamespace(
            root=self.root,
            build=self.root / "build",
            include=(include,),
            cppflags=(),
            default_compiler="c",
            compilers={"c": SimpleNamespace(cflags=())},
            version=versions.__getitem__,
        )
        self.host = Host.from_values(host_values(self.root), "compare")
        compiler = self.project.compilers["c"]
        compiler.cc = self.host.cpp
        compiler.sha256 = self.root / "compiler.pin"
        compiler.sha256.write_text("fixture compiler pin")
        compiler.kind = "gnu"
        self.project.gnu_asflags = ()
        self.project.unit_flags = {}
        self.project.compiler_for = lambda unit: compiler
        self.project.versions = ("us", "eu")

    def key(self, version: str = "us") -> str:
        return facts.source_key(self.project, self.host, ("alpha", self.source, version), facts.Snapshot(self.project))

    def test_key_changes_with_its_inputs_only(self) -> None:
        include = self.root / "include"
        base = self.key()
        cases = [
            ("source bytes", self.source, self.source.read_text() + "/* edit */\n", True),
            ("included header", include / "used.h", "typedef long Used;\n", True),
            ("nested header", include / "nested.h", "typedef long Nested;\n", True),
            ("unrelated header", include / "unrelated.h", "typedef long Unrelated;\n", False),
        ]
        for label, path, text, changes in cases:
            with self.subTest(label):
                original = path.read_text()
                path.write_text(text)
                self.assertEqual(self.key() != base, changes)
                path.write_text(original)
                self.assertEqual(self.key(), base)

    def test_generated_header_bytes_stay_in_the_key(self) -> None:
        """Consumed contracts are read from generated headers: facts kept across a changed one go stale."""
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include" / "used.h"
        with patch.object(index, "headers", return_value=frozenset({used})):
            base = self.key()
            original = used.read_text()
            used.write_text(original + "typedef long Used2;\n")
            self.assertNotEqual(self.key(), base)
            used.write_text(original)

    def test_key_names_the_version(self) -> None:
        self.assertNotEqual(self.key("us"), self.key("eu"))


class UnitKeyTests(SourceKeyTests):
    """A source part keys on generated headers' interface only: a land that changes their declarations,
    layouts or (void) spellings (the second types pass) re-extracts no source."""

    def setUp(self) -> None:
        super().setUp()
        self.source.write_text('#include "used.h"\nUsed alpha(void) { return 1; }\n')

    def key(self, version: str = "us") -> str:  # type: ignore[override]
        return facts.unit_key(self.project, self.host, self.source, version, facts.Snapshot(self.project))

    def test_generated_header_changes(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include" / "used.h"
        original = used.read_text()
        cases = [
            # (label, generated header text, key changes)
            ("a declaration added", original + "extern int added;\n", False),
            ("a prototype respelled (void)", original + "extern void f(void);\n", False),
            ("a layout changed", original + "struct Shape { int a; };\n", False),
            ("a comment", original + "/* typedef long Commented; */\n", False),
            ("a typedef of a name the source never spells", original + "typedef long Used2;\n", False),
            ("a typedef retargeted", original.replace("typedef int Used;", "typedef long Used;"), True),
            ("a directive added", original + "#define USED 1\n", True),
            ("an aggregate typedef of an unspelled name", original + "typedef struct { int a; } Anon;\n", False),
        ]
        with patch.object(index, "headers", return_value=frozenset({used})):
            base = self.key()
            for label, text, changes in cases:
                with self.subTest(label):
                    used.write_text(text)
                    self.assertEqual(self.key() != base, changes)
                    used.write_text(original)
            self.assertEqual(self.key(), base)

    def test_generated_include_edges_with_no_selected_interface_do_not_churn(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include/used.h"
        nested = self.root / "include/nested.h"
        unrelated = self.root / "include/unrelated.h"
        guard = "#ifndef GENERATED_H\n#define GENERATED_H\n"
        used.write_text(guard + "typedef int Used;\n#endif\n")
        nested.write_text("#ifndef NESTED_H\n#define NESTED_H\ntypedef long Other;\n#endif\n")
        with patch.object(index, "headers", return_value=frozenset({used, nested, unrelated})):
            before = self.key()
            used.write_text(guard + '#include "nested.h"\ntypedef int Used;\n#endif\n')
            self.assertEqual(self.key(), before)
            nested.write_text("typedef char Other;\n")
            self.assertEqual(self.key(), before)
            nested.write_text("#define COUNT 2\n")
            self.assertNotEqual(self.key(), before)

    def test_added_include_selecting_a_transitive_typedef_changes_key(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include/used.h"
        nested = self.root / "include/nested.h"
        used.write_text("typedef Nested Used;\n")
        with patch.object(index, "headers", return_value=frozenset({used, nested})):
            before = self.key()
            used.write_text('#include "nested.h"\ntypedef Nested Used;\n')
            self.assertNotEqual(self.key(), before)
            before = self.key()
            nested.write_text("typedef long Nested;\n")
            self.assertNotEqual(self.key(), before)

    def test_referenced_guard_and_conditional_directives_remain_inputs(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include/used.h"
        self.source.write_text('#include "used.h"\n#ifdef GUARD_H\nint alpha;\n#endif\n')
        with patch.object(index, "headers", return_value=frozenset({used})):
            used.write_text("#ifndef GUARD_H\n#define GUARD_H\n#endif\n")
            before = self.key()
            used.write_text("#ifndef OTHER_H\n#define OTHER_H\n#endif\n")
            self.assertNotEqual(self.key(), before)
            used.write_text("#if VERSION_US\ntypedef int Unused;\n#endif\n")
            before = self.key()
            used.write_text("#if VERSION_EU\ntypedef int Unused;\n#endif\n")
            self.assertNotEqual(self.key(), before)

    def test_generated_header_bytes_stay_in_the_key(self) -> None:
        """Overridden: generated header bytes are the header layer's input, not the source part's."""

    def test_a_spelled_typedef_is_read(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include" / "used.h"
        original = used.read_text()
        with patch.object(index, "headers", return_value=frozenset({used})):
            self.source.write_text(self.source.read_text() + "Used2 other;\n")
            spelled = self.key()
            used.write_text(original + "typedef long Used2;\n")
            self.assertNotEqual(self.key(), spelled)

    def test_a_spelled_typedef_reads_the_aggregate_it_names(self) -> None:
        """Used is typedef'd to a header's struct: the struct's body is read through the typedef's own text."""
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include" / "used.h"
        used.write_text("struct Shape { int a; };\ntypedef struct Shape Used;\n")
        with patch.object(index, "headers", return_value=frozenset({used})):
            base = self.key()
            used.write_text("struct Shape { long a; };\ntypedef struct Shape Used;\n")
            self.assertNotEqual(self.key(), base)
            used.write_text("struct Shape { int a; };\ntypedef struct Shape Used;\nstruct Other { int b; };\n")
            self.assertEqual(self.key(), base)


class InterfaceTests(TempCase):
    TEXT = (
        "#ifndef GUARD\n#define GUARD\n/* typedef int Hidden; */\nextern int value;\n"
        "typedef struct Pair {\n    int a;\n} Pair;\nstruct Body { int b; };\n"
        "typedef int (*Fn)(int);\ntypedef enum { A, B } E;\ntypedef unsigned int u32;\n#endif\n"
    )
    DIRECTIVES: ClassVar[list[str]] = ["#ifndef GUARD", "#define GUARD", "#endif"]

    def lines(self, *names: str) -> list[str]:
        return facts.interface(self.TEXT, names).splitlines()

    def test_no_names_omits_the_unreferenced_guard(self) -> None:
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.lines("GUARD"), self.DIRECTIVES)
        self.assertEqual(self.lines("value", "int", "unsigned", "a", "b", "Hidden"), [])

    def test_a_name_selects_the_statement_that_declares_it(self) -> None:
        self.assertEqual(self.lines("Pair"), ["typedef struct Pair { int a; } Pair;"])
        self.assertEqual(self.lines("Body"), ["struct Body { int b; };"])
        self.assertEqual(self.lines("Fn"), ["typedef int (*Fn)(int);"])
        self.assertEqual(self.lines("u32"), ["typedef unsigned int u32;"])

    def test_an_enumerator_selects_its_enum(self) -> None:
        self.assertEqual(self.lines("B"), ["typedef enum { A, B } E;"])

    def test_statements_keep_the_text_order(self) -> None:
        self.assertEqual(
            self.lines("u32", "Pair"), ["typedef struct Pair { int a; } Pair;", "typedef unsigned int u32;"]
        )


class HeaderKeyTests(SourceKeyTests):
    """A header part keys on its own bytes and its generated includes' shape: a publish that changes only their
    declarations re-extracts no part of a header that includes them."""

    def setUp(self) -> None:
        super().setUp()
        self.header = self.root / "include" / "consumer.h"
        self.header.write_text('#include "used.h"\nstruct Holder { Used u; };\n')

    def key(self, version: str = "us") -> str:  # type: ignore[override]
        return facts.header_key(self.project, self.host, self.header, version, facts.Snapshot(self.project))

    def test_key_changes_with_its_inputs_only(self) -> None:
        """Overridden: the source's bytes are no input of a header part."""

    def test_generated_header_bytes_stay_in_the_key(self) -> None:
        """Overridden: a generated include counts by its interface for the names this header spells."""

    def test_generated_include_changes(self) -> None:
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include" / "used.h"
        original = used.read_text()
        cases = [
            ("a declaration added", original + "extern int added;\n", False),
            ("a prototype respelled (void)", original + "extern void f(void);\n", False),
            ("a comment", original + "/* struct Gone { int a; }; */\n", False),
            ("an aggregate the header never spells", original + "struct Shape { int a; };\n", False),
            ("an enumerator the header never spells", original + "enum { COUNT = 4 };\n", False),
            ("a typedef of a name the header never spells", original + "typedef struct Shape Used2;\n", False),
            ("a typedef retargeted", original.replace("typedef int Used;", "typedef long Used;"), True),
            ("a directive added", original + "#define USED 1\n", True),
        ]
        with patch.object(index, "headers", return_value=frozenset({used, self.header})):
            base = self.key()
            for label, text, changes in cases:
                with self.subTest(label):
                    used.write_text(text)
                    self.assertEqual(self.key() != base, changes)
                    used.write_text(original)
            self.assertEqual(self.key(), base)
            self.header.write_text(self.header.read_text() + "extern int own;\n")
            self.assertNotEqual(self.key(), base)  # its own bytes always count, generated or not

    def test_a_spelled_aggregate_is_read(self) -> None:
        """Holder embeds Shape by value: a change of Shape's body (and of a name its members spell) re-keys."""
        from unittest.mock import patch

        from unbake.layout import index

        used = self.root / "include" / "used.h"
        used.write_text("struct Shape { Used a; };\ntypedef int Used;\n")
        self.header.write_text('#include "used.h"\nstruct Holder { struct Shape s; };\n')
        with patch.object(index, "headers", return_value=frozenset({used, self.header})):
            base = self.key()
            used.write_text("struct Shape { Used a; Used b; };\ntypedef int Used;\n")
            self.assertNotEqual(self.key(), base)
            used.write_text("struct Shape { Used a; };\ntypedef long Used;\n")
            self.assertNotEqual(self.key(), base)
            used.write_text("struct Shape { Used a; };\ntypedef int Used;\nstruct Unspelled { int z; };\n")
            self.assertEqual(self.key(), base)


class ComputedIncludeTests(TempCase):
    def test_a_computed_include_is_a_preprocessor_input(self):
        self.assertEqual(facts.interface("#include SELECTED_HEADER\n", []), "#include SELECTED_HEADER")
        self.assertEqual(facts.interface('#include "literal.h"\n', []), "")
