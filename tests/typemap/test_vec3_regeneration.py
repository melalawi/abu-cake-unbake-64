"""Small real RageWars payload: inference cannot erase a published Vec3 dependency."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.cdecl import declarations
from unbake.config import Held
from unbake.layout import apply, header_step, index, map
from unbake.typemap import database, declaration_evidence, regeneration

FIXTURE = Path(__file__).parent / "fixtures/ragewars_vec3"
FUNCTION = "func_8020CD74_de"


class Vec3RegenerationTests(ProjectCase):
    def setUp(self):
        super().setUp()
        for path in FIXTURE.rglob("*"):
            if path.is_file() and path.suffix in (".h", ".c"):
                target = self.project.root / path.relative_to(FIXTURE)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        ownership = map.Map(2, (map.Group("code_8020AF9C", "span_1000", "default", (FUNCTION,)),))
        self.patch = patch.object(map, "load", return_value=ownership)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.source = self.project.src / (FUNCTION + ".c")
        self.home = self.project.include[0] / "common/types_8a8189af7b05.h"

    def test_real_regeneration_without_vec3_facts_retains_the_typedef_and_definition(self):
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        before = {path: path.read_bytes() for path in self.project.include[0].rglob("*.h")}
        try:
            outputs = database._render(self.project, value, None, regeneration.Session(self.project, None))
        except Held as error:
            self.assertIn("Vec3", error.reason)
            self.assertIn(FUNCTION + ".c", error.reason)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            return
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
        self.assertEqual({path: path.read_bytes() for path in before}, before)

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
            header_step.plan(self.project, outputs)
