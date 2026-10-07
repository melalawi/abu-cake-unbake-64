"""Small real RageWars payload: inference cannot erase a published Vec3 dependency."""

import json
import shutil
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake.cdecl import declarations
from unbake.config import Held
from unbake.layout import apply, header_loss, header_step, index, map
from unbake.typemap import database, declaration_evidence, regeneration

FIXTURE = TESTS / "typemap/fixtures/ragewars_vec3"
FUNCTION = "func_8020CD74_de"


class Vec3RegenerationTests(ProjectCase):
    def setUp(self):
        super().setUp()
        for path in FIXTURE.rglob("*"):
            if path.is_file() and path.suffix in (".h", ".c"):
                target = self.project.root / path.relative_to(FIXTURE)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        ownership = map.Map(
            2,
            (
                map.Group("code_8020AF9C", "span_1000", "default", (FUNCTION,)),
                map.Group("code_80271B18", "span_1000", "default", ("func_80272018_de",)),
            ),
        )
        self.patch = patch.object(map, "load", return_value=ownership)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.source = self.project.src / (FUNCTION + ".c")
        self.home = self.project.include[0] / "common/types_8a8189af7b05.h"

    def test_real_regeneration_without_vec3_facts_retains_the_typedef_and_definition(self):
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        before = {path: path.read_bytes() for path in self.project.include[0].rglob("*.h")}
        outputs = database._render(self.project, value, None, regeneration.Session(self.project, None))
        aliases = set().union(
            *(declarations(data.decode()).typedefs for path, data in outputs.items() if path.suffix == ".h")
        )
        self.assertIn("Vec3", aliases)
        # Layout's actual source rewrite must import the new home, so pass two
        # sees both the typedef and its complete definition through real includes.
        rewritten = apply.source(
            self.project,
            self.source.read_text(),
            FUNCTION,
            outputs,
            lookup=json.loads(outputs[index.path(self.project)]),
        )
        bodies = "\n".join(apply.imported(rewritten, self.project.include[0], outputs))
        self.assertIn("Vec3", declarations(bodies).typedefs)
        self.assertIn("struct Vec3 {", bodies)
        staged = {**outputs, self.source: rewritten.encode()}
        for source in self.project.src.glob("*.c"):
            staged[source] = apply.source(
                self.project,
                source.read_text(),
                source.stem,
                outputs,
                lookup=json.loads(outputs[index.path(self.project)]),
            ).encode()
        header_loss.check(self.project, staged, obsolete=index.headers(self.project) - staged.keys())
        rendered = "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        retained, _ = declaration_evidence.published_snapshot(
            self.project, sources={self.source: self.source.read_text()}
        )
        for statement in retained.values():
            self.assertIn(statement.strip(), rendered)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_private_same_named_tag_does_not_remove_another_consumers_contract(self):
        source = self.project.src / "func_80272018_de.c"
        source.write_text("struct Vec3 { int local; }; void func_80272018_de(void) {}\n")
        self.test_real_regeneration_without_vec3_facts_retains_the_typedef_and_definition()

    def test_authored_forward_tag_keeps_the_published_alias_reachable(self):
        forward = self.project.include[0] / "forward.h"
        forward.write_text("struct Vec3;\n")
        self.source.write_text('#include "forward.h"\n' + self.source.read_text())
        self.test_real_regeneration_without_vec3_facts_retains_the_typedef_and_definition()

    def test_proven_incompatible_vec3_names_both_contracts(self):
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["structs"]["Vec3"] = {
            "state": "known",
            "generated": True,
            "declaration": "struct Vec3 { int incompatible; };",
            "provenance": {"kind": "proven", "function": "replacement"},
        }
        with self.assertRaisesRegex(
            Held, r"(?s)headers.declaration: Vec3: published.*f32 x;.*incompatible with proven.*int incompatible;"
        ):
            database._render(self.project, value, None, regeneration.Session(self.project, None))

    def test_changed_installed_contract_invalidates_cached_render(self):
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        first = regeneration.Session(self.project, None)
        first.render(value, lambda: database._render(self.project, value, None, first))
        self.home.write_text(self.home.read_text().replace("    f32 x;", "    f32 renamed_x;", 1))
        second = regeneration.Session(self.project, None)
        self.assertNotEqual(first.inputs, second.inputs)
        fresh = deepcopy(value)
        outputs = second.render(fresh, lambda: database._render(self.project, fresh, None, second))
        self.assertIn(
            "f32 renamed_x;", "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        )

    def test_compatible_proven_layout_keeps_the_installed_spelling(self):
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["structs"]["Vec3"] = {
            "state": "known",
            "generated": True,
            "declaration": "struct Vec3 { float x; float y; float z; };",
            "provenance": {"kind": "proven", "function": "replacement"},
        }
        outputs = database._render(self.project, value, None, regeneration.Session(self.project, None))
        bodies = "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        self.assertIn("struct Vec3 {\n    f32 x;\n    f32 y;\n    f32 z;\n};", bodies)
        self.assertIn("typedef struct Vec3 Vec3;", bodies)

    def test_full_dependency_closure_keeps_aliases_constants_globals_and_prototypes(self):
        statements = (
            "#define VECTOR_COUNT 3",
            "enum { VECTOR_FLAGS = 1 };",
            "typedef Vec3 Vector;",
            "typedef Vector *VectorPtr;",
            "struct Bundle { Vector entries[VECTOR_COUNT]; };",
            "typedef struct Bundle Bundle;",
            "extern Bundle *bundle;",
            "extern VectorPtr query_vec(Bundle *, int);",
        )
        self.home.write_text(self.home.read_text().replace("\n#endif", "\n" + "\n".join(statements) + "\n#endif"))
        self.source.write_text(
            self.source.read_text().replace(
                "    Vec3 position;", "    Vec3 position;\n    VectorPtr selected = query_vec(bundle, VECTOR_FLAGS);"
            )
        )
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        outputs = database._render(self.project, value, None, regeneration.Session(self.project, None))
        bodies = "\n".join(data.decode() for path, data in outputs.items() if path.suffix == ".h")
        for statement in statements:
            self.assertIn(statement, bodies)
        lookup = json.loads(outputs[index.path(self.project)])
        staged = dict(outputs)
        for source in self.project.src.glob("*.c"):
            staged[source] = apply.source(
                self.project, source.read_text(), source.stem, outputs, lookup=lookup
            ).encode()
        header_loss.check(self.project, staged, obsolete=index.headers(self.project) - staged.keys())

    def test_proven_incompatible_global_names_both_contracts(self):
        self.home.write_text(self.home.read_text().replace("\n#endif", "\nextern Vec3 canonical_point;\n#endif"))
        self.source.write_text(
            self.source.read_text().replace("    Vec3 position;", "    Vec3 position = canonical_point;")
        )
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["globals"]["canonical_point"] = {
            "state": "known",
            "declaration": "extern int canonical_point;",
            "provenance": [{"kind": "proven", "function": "replacement"}],
        }
        with self.assertRaisesRegex(
            Held, r"(?s)published.*extern Vec3 canonical_point;.*proven.*extern int canonical_point;"
        ):
            database._render(self.project, value, None, regeneration.Session(self.project, None))

    def test_cached_render_restores_the_retained_declarations(self):
        session = regeneration.Session(self.project, None)
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        first = session.render(value, lambda: database._render(self.project, value, None, session))
        self.assertTrue(value["published_declarations"])
        fresh = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        second = session.render(fresh, lambda: self.fail("identical regeneration should reuse the cache"))
        self.assertEqual(second, first)
        for field in ("published_declarations", "published_homes"):
            self.assertEqual(fresh[field], value[field])

    def test_real_typedef_loss_is_refused_with_source_before_install(self):
        before = {path: path.read_bytes() for path in self.project.include[0].rglob("*.h")}
        outputs = {self.home: self.home.read_bytes().replace(b"typedef struct Vec3 Vec3;", b"")}
        with self.assertRaisesRegex(Held, r"would remove Vec3 used by published C .*func_8020CD74_de.c"):
            apply.install(self.project, outputs)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_local_same_named_tag_in_another_unit_does_not_hide_vec3(self):
        other = self.project.src / "scratch.c"
        other.write_text("struct Vec3 { int local; }; void scratch(void) {}\n")
        rows = [
            SimpleNamespace(kind="c", name=FUNCTION, path=FUNCTION),
            SimpleNamespace(kind="c", name="scratch", path="scratch"),
        ]
        with patch("unbake.layout.split.functions", return_value=rows):
            components, _ = declaration_evidence.published_snapshot(self.project)
        self.assertIn("typedef struct Vec3 Vec3;", "\n".join(components.values()))

    def test_real_layout_guard_recognizes_typedef_separately_from_tag(self):
        outputs = {self.home: self.home.read_bytes().replace(b"typedef struct Vec3 Vec3;", b"")}
        with self.assertRaisesRegex(Held, r"would remove Vec3 used by published C .*func_8020CD74_de.c"):
            header_step.plan(self.project, outputs, self.host)
