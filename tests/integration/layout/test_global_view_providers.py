"""Retained native object views stay scoped to their actual C consumers."""

import hashlib
import json
import shutil
import subprocess
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout import apply, headers, index, redeclarations
from unbake.layout.headers import Layout
from unbake.layout.map import Group, Map
from unbake.project.headers import Graph
from unbake.typemap.split import required_providers

FIXTURE = (TESTS / "layout/test_global_view_providers.py").parents[1] / "fixtures/ragewars_global_views"
GLOBAL = "D_80140F80"


class GlobalViewProviderTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def setUp(self):
        super().setUp()
        self.cc = shutil.which("gcc")
        self.cpp = shutil.which("cpp")
        if self.cc is None or self.cpp is None:
            self.skipTest("native cpp/gcc required")
        self.include = self.project.include[0]
        self.contents = {}
        for name in ("types.h", "scalar.h", "byte.h", "menu_layout.h", "menu.h", "world.h"):
            path = self.include / (name if name == "types.h" else "shared/" + name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((FIXTURE / name).read_bytes())
            self.contents[path] = path.read_text()
        self.sources = {
            self.project.src / (unit + ".c"): (FIXTURE / name).read_text()
            for unit, name in (("alpha", "scalar.c"), ("beta", "byte.c"), ("gamma", "menu.c"), ("delta", "world.c"))
        }
        self.ownership = Map(32, (Group("views", "main", "default", tuple(p.stem for p in self.sources)),))
        self.subprocesses = []

    def layout(self, sources=None):
        return Layout(
            self.contents,
            self.contents,
            self.include,
            ownership=self.ownership,
            sources=self.sources if sources is None else sources,
            authored=set(self.contents),
        )

    def projected(self, layout, source, text):
        outputs = {**layout.headers, index.path(self.project): index.encoded(layout.index)}
        return apply.source(
            self.project,
            text,
            source.stem,
            outputs,
            ownership=self.ownership,
            lookup=layout.index,
            previous=set(layout.index["headers"]),
        )

    def install_headers(self, layout):
        for path, data in layout.headers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def native(self, text, unit, version):
        expanded = subprocess.run(
            [
                self.cpp,
                "-P",
                "-undef",
                "-nostdinc",
                "-D_LANGUAGE_C",
                "-D__GNUC__=2",
                "-DVERSION_" + version.upper().replace("-", "_"),
                "-I" + str(self.include),
                "-x",
                "c",
                "-",
            ],
            input=text,
            capture_output=True,
            text=True,
        )
        self.subprocesses.append((unit, version, "cpp"))
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        compiled = subprocess.run(
            [self.cc, "-m32", "-std=gnu89", "-Werror=implicit-function-declaration", "-fsyntax-only", "-x", "c", "-"],
            input=expanded.stdout,
            capture_output=True,
            text=True,
        )
        self.subprocesses.append((unit, version, "cc"))
        return compiled

    def test_exact_real_declarations_layouts_and_body_expression_digests(self):
        for name, row in json.loads((FIXTURE / "provenance.json").read_text()).items():
            self.assertEqual(hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest(), row["slice_sha256"])

    def test_generated_group_does_not_mix_real_scalar_byte_menu_and_replay_views(self):
        with (
            patch.object(headers, "required_providers", wraps=required_providers) as selected,
            patch.object(headers, "declarations", wraps=headers.declarations) as parsed,
            patch.object(Graph, "closure", autospec=True, side_effect=Graph.closure) as closures,
        ):
            layout = self.layout()
        self.install_headers(layout)
        # Source adoption must succeed before checking work counts. Main's
        # group header imports all views and fails here with s32 vs char.
        first = next(iter(self.sources))
        self.projected(layout, first, self.sources[first])
        self.assertEqual(selected.call_count, 4)
        self.assertEqual(parsed.call_count, 7)
        self.assertEqual(closures.call_count, 8)
        for call, name in zip(selected.call_args_list, ("scalar.h", "byte.h", "menu.h", "world.h"), strict=True):
            self.assertIn(self.include / "shared" / name, call.kwargs["preferred"])
        module = layout.headers[self.include / "main/views.h"].decode()
        self.assertNotIn('#include "shared/', module)
        self.assertNotIn(GLOBAL, module)
        for source, text in self.sources.items():
            projected = self.projected(layout, source, text)
            self.assertEqual(projected[projected.index("{") :], text[text.index("{") :])
            self.assertNotIn(GLOBAL, layout.index["symbols"])
            for version in self.versions:
                result = self.native(projected, source.stem, version)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.subprocesses,
            [(p.stem, v, step) for p in self.sources for v in self.versions for step in ("cpp", "cc")],
        )
        self.assertEqual(len(self.subprocesses), 40)

    def test_existing_transitive_authored_wrapper_remains_source_scoped(self):
        wrapper = self.include / "wrapper.h"
        wrapper.write_text('#include "shared/menu.h"\n')
        self.contents[wrapper] = wrapper.read_text()
        source = self.project.src / "gamma.c"
        text = self.sources[source].replace('"shared/menu.h"', '"wrapper.h"')
        layout = self.layout({source: text})
        self.assertNotIn('#include "shared/', layout.headers[self.include / "main/views.h"].decode())
        self.install_headers(layout)
        projected = self.projected(layout, source, text)
        self.assertIn('#include "wrapper.h"', projected)
        result = self.native(projected, "gamma", "eu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.subprocesses), 2)

    def test_unanchored_address_use_keeps_the_real_conflict_guard(self):
        source = self.project.src / "alpha.c"
        text = "void *alpha(void) { return &D_80140F80; }\n"
        layout = self.layout({source: text})
        with self.assertRaisesRegex(Held, "layout.redeclaration.D_80140F80: shared conflict"):
            self.projected(layout, source, text)
        module = layout.headers[self.include / "main/views.h"].decode()
        self.assertIn('#include "shared/scalar.h"', module)
        self.assertIn('#include "shared/byte.h"', module)

    def test_incompatible_existing_imports_still_refuse_without_selecting_a_winner(self):
        source = self.project.src / "alpha.c"
        text = '#include "shared/scalar.h"\n' + self.sources[self.project.src / "beta.c"].replace("beta(", "alpha(")
        layout = self.layout({source: text})
        with self.assertRaisesRegex(Held, "layout.redeclaration.D_80140F80: shared conflict"):
            self.projected(layout, source, text)
        result = self.native(text, "alpha", "de")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(GLOBAL, result.stderr)
        self.assertEqual(len(self.subprocesses), 2)

    def test_scalar_provider_cannot_satisfy_a_real_replay_field_read(self):
        source = self.project.src / "delta.c"
        text = self.sources[source].replace('"shared/world.h"', '"shared/scalar.h"')
        layout = self.layout({source: text})
        self.install_headers(layout)
        result = self.native(self.projected(layout, source, text), "delta", "us-rev1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("replay", result.stderr)
        self.assertEqual(len(self.subprocesses), 2)

    def test_proven_scalar_and_structured_contracts_remain_incompatible(self):
        scalar = (FIXTURE / "scalar.h").read_text()
        world = (FIXTURE / "world.h").read_text()
        with self.assertRaisesRegex(Held, "shared conflict"):
            redeclarations.strip("", [scalar, world])
        self.assertFalse(redeclarations.equivalent("extern s32 D_80140F80;", "extern char D_80140F80;", {"s32": "int"}))
