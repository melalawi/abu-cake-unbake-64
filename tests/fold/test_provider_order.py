"""Real RageWars measured and Vec3 provider slices compile in the emitted order."""

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import process
from unbake.config import Compiler
from unbake.decomp.draft_context import ordered_headers
from unbake.fold import imports
from unbake.layout.headers import Layout
from unbake.layout.map import Group, Map
from unbake.typemap import database, regeneration

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_declaration_order"


class ProviderOrderTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.cpp, self.cc = shutil.which("cpp"), shutil.which("cc")
        if self.cpp is None or self.cc is None:
            self.skipTest("native cpp and C compiler required")
        self.include = self.project.include[0]
        self.contents = {}
        for home, fixture in (
            ("types.h", "scalar.h"),
            ("common/measured.h", "measured.h"),
            ("common/vec.h", "vec.h"),
            ("shared/placed.h", "placed.h"),
        ):
            path = self.include / home
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((FIXTURE / fixture).read_bytes())
            self.contents[path] = path.read_text()
        self.native_calls = 0

    def native(self, text):
        expanded = subprocess.run(
            [self.cpp, "-P", "-I" + str(self.include), "-x", "c", "-"],
            input=text,
            capture_output=True,
            text=True,
        )
        self.native_calls += 1
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        compiled = subprocess.run(
            [self.cc, "-std=gnu89", "-fsyntax-only", "-x", "c", "-"],
            input=expanded.stdout,
            capture_output=True,
            text=True,
        )
        self.native_calls += 1
        return expanded.stdout, compiled

    def resolve(self, source):
        return imports.resolve(self.project, SimpleNamespace(texts=self.contents), source, "func_8043705C_de")

    def test_exact_provider_slice_digests(self):
        for name, digest in json.loads((FIXTURE / "digests.json").read_text()).items():
            self.assertEqual(hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest(), digest)

    def test_folded_measured_header_gets_its_existing_later_scalar_provider_first(self):
        source = (FIXTURE / "folded.c").read_text()
        _, broken = self.native(source)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("s32", broken.stderr)
        with patch.object(imports, "ordered_headers", wraps=ordered_headers) as order:
            result = self.resolve(source)
        self.assertEqual(order.call_count, 1)
        self.assertLess(result.index('"types.h"'), result.index('"common/measured.h"'))
        self.assertEqual(result.count('#include "types.h"'), 1)
        self.assertEqual(result.count('#include "common/measured.h"'), 1)
        self.assertTrue(result.endswith(source[source.index("void func_") :]))
        self.assertEqual(self.resolve(result), result)
        expanded, compiled = self.native(result)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertLess(expanded.index("typedef signed int s32;"), expanded.index("struct Measured_"))
        self.assertEqual(self.native_calls, 4)

    def test_missing_scalar_is_recovered_before_the_measured_header(self):
        source = (FIXTURE / "folded.c").read_text().replace('#include "types.h"\n', "")
        result = self.resolve(source)
        self.assertLess(result.index('"types.h"'), result.index('"common/measured.h"'))
        _, compiled = self.native(result)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertEqual(self.native_calls, 2)

    def test_existing_wrapper_supplies_the_scalar_without_an_extra_import(self):
        path = self.include / "wrapper.h"
        path.write_text('#include "types.h"\n')
        self.contents[path] = path.read_text()
        source = (FIXTURE / "folded.c").read_text().replace('"types.h"', '"wrapper.h"')
        result = self.resolve(source)
        self.assertLess(result.index('"wrapper.h"'), result.index('"common/measured.h"'))
        self.assertNotIn('#include "types.h"', result)
        self.assertEqual(self.resolve(result), result)
        _, compiled = self.native(result)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertEqual(self.native_calls, 2)

    def test_configuration_comments_and_conditional_imports_keep_their_bytes(self):
        source = "#define CONFIG 1\n/* source note */\n" + (FIXTURE / "folded.c").read_text()
        source = source.replace('"types.h"\n', '"types.h"\n#if CONFIG\n#include "common/vec.h"\n#endif\n')
        result = self.resolve(source)
        self.assertTrue(result.startswith("#define CONFIG 1\n/* source note */\n"))
        self.assertIn('#if CONFIG\n#include "common/vec.h"\n#endif\n', result)
        self.assertEqual(result.count('#include "common/vec.h"'), 1)
        _, compiled = self.native(result)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_proposed_home_orders_the_actual_vec3_and_placed_declarations(self):
        payload = self.include / ".payload.h"
        placed = next(
            line for line in self.contents[self.include / "shared/placed.h"].splitlines() if "Shared_Placed" in line
        )
        vec = self.contents[self.include / "common/vec.h"]
        vec = "\n".join(line for line in vec.splitlines() if not line.startswith("#"))
        source = placed + "\n" + vec + "\n" + (FIXTURE / "scalar.h").read_text()
        source = "\n".join(line for line in source.splitlines() if not line.startswith("#"))
        result = Layout(
            {payload: source},
            {payload: source},
            self.include,
            ownership=Map(32, (Group("g", "main", "default", ("alpha",)),)),
            sources={self.project.src / "alpha.c": "int alpha(void) {return 0;}"},
        )
        body = result.headers[result.homes[payload]].decode()
        self.assertLess(body.index("struct Vec3 {"), body.index("struct Shared_Placed {"))
        _, compiled = self.native(body)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_current_explicit_vec3_closure_validates_with_one_real_cpp(self):
        compiler = Compiler("gcc-2.8.1-sn64", "gnu", Path(self.cc), Path("as"), (), Path("sha256"))
        project = replace(
            self.project, compilers={compiler.id: compiler}, default_compiler=compiler.id, units={}, cppflags=("-P",)
        )
        outputs = {self.include / "common/vec.h": self.contents[self.include / "common/vec.h"].encode()}
        authored = {path: text for path, text in self.contents.items() if path.name != "measured.h"}
        contents, closures, _ = regeneration.validation_inputs(project, outputs, "", authored=authored)
        job = database._Validation(
            project, SimpleNamespace(cpp=Path(self.cpp)), "us", contents, closures, [], frozenset()
        )
        with patch.object(process, "run_tool", wraps=process.run_tool) as run:
            context_key = database._validate_version(job)
        self.assertTrue(context_key)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(list((project.build / "types").glob("held-header-context-*.c")))
        _, compiled = self.native('#include "shared/placed.h"\n')
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
