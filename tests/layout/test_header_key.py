"""headers keys on the installed type solution, not on the types step's input key."""

from types import SimpleNamespace

from tests.kit import TempCase
from unbake.layout import header_step
from unbake.typemap import types_db


class HeaderKeyTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "layout.toml").write_text("cap = 1\n")
        (self.root / "include").mkdir()
        (self.root / "src").mkdir()
        self.project = SimpleNamespace(
            root=self.root, build=self.root / "build", include=(self.root / "include",), src=self.root / "src"
        )

    def solve(self, revision: int, functions: dict) -> str:
        value = {
            **dict.fromkeys(types_db.REUSE_META, "fixture"),
            "revision": revision,
            "functions": functions,
            "globals": {},
            "structs": {},
            "arrays": {},
        }
        database = types_db.path(self.project)
        staged, _ = types_db.stage(database, types_db.encode(value), {})
        types_db.install(database, staged)
        return header_step.input_key(self.project)

    def test_a_solve_that_changes_nothing_but_its_revision_reruns_nothing(self) -> None:
        missing = header_step.input_key(self.project)
        first = self.solve(1, {"f": {"type": "int f(void);"}})
        for label, revision, functions, same in [
            ("solved again, identical", 2, {"f": {"type": "int f(void);"}}, True),
            ("a prototype changed", 3, {"f": {"type": "void f(void);"}}, False),
        ]:
            with self.subTest(label):
                self.assertEqual(self.solve(revision, functions) == first, same)
        self.assertNotEqual(missing, first)
