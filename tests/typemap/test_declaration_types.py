"""Historical declaration reuse retains source isolation and type precedence."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.project.config import Held
from unbake.typemap import declarations, solver, storage


class DeclarationReuseTests(unittest.TestCase):
    def test_abstract_types_preserve_shape_and_do_not_copy_named_bodies(self):
        cases = [
            ("typedef int Word;", "int"),
            ("typedef const unsigned int *Word;", "const unsigned int *"),
            ("typedef int Word[4];", "int [4]"),
            ("typedef int (*Word)(int value);", "int (*)(int)"),
            ("typedef struct Big { int value; } *Word;", "struct Big *"),
            ("typedef union Big { int value; float other; } Word;", "union Big"),
            ("typedef struct { int value; } Word;", "struct { int value; }"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                node = declarations.c_parser.CParser().parse(source).ext[0].type
                before = copy.deepcopy(node)
                self.assertEqual(declarations._type(node), expected)
                self.assertEqual(
                    declarations.c_generator.CGenerator().visit(node),
                    declarations.c_generator.CGenerator().visit(before),
                )

        class Uncopied:
            def __deepcopy__(self, memo):
                raise AssertionError("copied discarded body")

        field = Uncopied()
        node = declarations.c_ast.Struct("Big", [field])
        self.assertEqual(declarations._type(node), "struct Big")
        self.assertEqual(node.decls, [field])

    def test_independent_summary_changes_are_refused_before_whole_program_work(self):
        for case in ("digest", "identity", "large_missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                project = SimpleNamespace(build=Path(tmp), checkout_id="local")
                directory = project.build / "types"
                directory.mkdir()
                database = directory / "database.json"
                database.write_bytes(b"database")
                if case == "large_missing":
                    with database.open("wb") as stream:
                        stream.truncate(65 * 1024 * 1024)
                else:
                    storage.write(
                        directory / "summary.json",
                        storage.encoded(
                            {
                                "schema": 1,
                                "checkout_id": "local",
                                "project_id": "wrong" if case == "identity" else "same",
                                "database_sha256": "wrong" if case == "digest" else storage.file_digest(database),
                            }
                        ),
                    )
                with (
                    patch.object(
                        storage, "identity", return_value={"schema": 1, "project_id": "same", "checkout_id": "local"}
                    ),
                    patch.object(declarations, "collect") as collect,
                    patch.object(solver, "infer") as infer,
                    patch("unbake.typemap.abi_facts.refine") as refine,
                    self.assertRaisesRegex(Held, "types.summary"),
                ):
                    solver.solve(project)
                collect.assert_not_called()
                infer.assert_not_called()
                refine.assert_not_called()
