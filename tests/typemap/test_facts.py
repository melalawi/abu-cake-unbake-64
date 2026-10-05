"""Per-source facts keys follow exactly what the facts depend on."""

from types import SimpleNamespace

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
            ("a typedef added", original + "typedef long Used2;\n", True),
            ("a typedef retargeted", original.replace("typedef int Used;", "typedef long Used;"), True),
            ("a directive added", original + "#define USED 1\n", True),
            ("a typedef of an aggregate body", original + "typedef struct { int a; } Anon;\n", True),
        ]
        with patch.object(index, "headers", return_value=frozenset({used})):
            base = self.key()
            for label, text, changes in cases:
                with self.subTest(label):
                    used.write_text(text)
                    self.assertEqual(self.key() != base, changes)
                    used.write_text(original)
            self.assertEqual(self.key(), base)

    def test_generated_header_bytes_stay_in_the_key(self) -> None:
        """Overridden: generated header bytes are the header layer's input, not the source part's."""

    def test_interface_lists_directives_and_typedefs(self) -> None:
        text = (
            "#ifndef GUARD\n#define GUARD\n/* typedef int Hidden; */\nextern int value;\n"
            "typedef struct Pair {\n    int a;\n} Pair;\nstruct Body { int b; };\n#endif\n"
        )
        self.assertEqual(
            facts.interface(text).split("\n"),
            ["#ifndef GUARD", "#define GUARD", "#endif", "typedef struct Pair { int a; } Pair;"],
        )
