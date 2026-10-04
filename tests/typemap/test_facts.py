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

    def test_key_names_the_version(self) -> None:
        self.assertNotEqual(self.key("us"), self.key("eu"))
