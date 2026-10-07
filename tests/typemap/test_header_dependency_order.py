"""Route 6: types precede consumers within and across generated headers."""

from pathlib import Path

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout.headers import Layout
from unbake.layout.map import Group, Map
from unbake.typemap import database, declarations, regeneration

FIXTURE = Path(__file__).parent / "fixtures/ragewars_header_order"
PROTOTYPE = (FIXTURE / "prototype.h").read_text()
SCALAR = (FIXTURE / "scalar.h").read_text()


class HeaderDependencyOrderTests(ProjectCase):
    def layout(self, contents, *, authored=()):
        return Layout(
            contents,
            contents,
            self.project.include[0],
            ownership=Map(32, (Group("g", "main", "default", ("alpha",)),)),
            sources={self.project.src / "alpha.c": "int alpha(void) {return func_8025F074_us(0,0,0,0,0,0);}"},
            authored=set(authored),
        )

    def test_real_return_typedef_precedes_the_prototype_in_the_same_home(self):
        payload = self.project.include[0] / ".payload.h"
        result = self.layout({payload: PROTOTYPE + SCALAR})
        body = result.headers[result.homes[payload]].decode()
        self.assertLess(body.index("typedef signed int s32;"), body.index("extern s32 func_8025F074_us"))
        parsed = declarations.extract(body, {})
        self.assertIn("func_8025F074_us", parsed["functions"])

    def test_field_and_callback_typedef_dependencies_are_also_ordered(self):
        payload = self.project.include[0] / ".payload.h"
        text = (
            PROTOTYPE + "struct Holder {Callback notify; Value value;};\n"
            "typedef s32 (*Callback)(Value *);\ntypedef struct Value {s32 word;} Value;\n" + SCALAR
        )
        result = self.layout({payload: text})
        body = result.headers[result.homes[payload]].decode()
        self.assertLess(body.index("typedef signed int s32;"), body.index("typedef struct Value"))
        self.assertLess(body.index("typedef struct Value"), body.index("typedef s32 (*Callback)"))
        self.assertLess(body.index("typedef s32 (*Callback)"), body.index("struct Holder {"))
        declarations.extract(body, {})

    def test_cross_header_provider_is_included_before_the_real_prototype(self):
        root = self.project.include[0]
        scalar, payload = root / "zz-scalar.h", root / ".payload.h"
        result = self.layout({payload: PROTOTYPE, scalar: SCALAR}, authored=(scalar,))
        body = result.headers[result.homes[payload]].decode()
        self.assertLess(body.index('#include "zz-scalar.h"'), body.index("extern s32 func_8025F074_us"))

    def validation(self, contents):
        self.project.build.mkdir(parents=True, exist_ok=True)
        return database._validate_version(
            database._Validation(
                self.project, None, "us", contents, {path: {path} for path in contents}, [], frozenset()
            )
        )

    def test_real_header_slice_validates_with_its_later_sorted_provider(self):
        root = self.project.include[0]
        self.validation({root / "aa-prototype.h": PROTOTYPE.encode(), root / "zz-scalar.h": SCALAR.encode()})

    def test_failed_context_is_saved_and_diagnostic_names_the_header_and_context(self):
        path = self.project.include[0] / "aa-prototype.h"
        with self.assertRaises(Held) as held:
            self.validation({path: PROTOTYPE.encode()})
        self.assertIn(str(path), held.exception.reason)
        contexts = list((self.project.build / "types").glob("held-header-context-us-*.c"))
        self.assertEqual(len(contexts), 1)
        context = contexts[0]
        self.assertIn(str(context), held.exception.reason)
        self.assertEqual(context.read_text().strip(), PROTOTYPE.strip())

    def test_declaration_parse_refusal_names_the_staged_header(self):
        path = self.project.include[0] / "aa-prototype.h"
        broken = b"extern signed func_8025F074_us unexpected(void);"
        with self.assertRaisesRegex(Held, "aa-prototype.h: header declaration"):
            regeneration.validation_inputs(self.project, {path: broken}, "", authored={})
