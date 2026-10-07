"""Native validation orders full RageWars provider closures before consumers."""

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import process
from unbake.config import Compiler, Held
from unbake.decomp.draft_context import ordered_headers
from unbake.typemap import database

FIXTURE = (TESTS / "typemap/test_native_vec3_context.py").parent / "fixtures/ragewars_native_vec3"
CONSUMER = """
f32 placed_x(struct Shared_Placed *placed) { return placed->pos.x; }
f32 player_z(struct Player *player) { return player->pos.z; }
struct Shared_Placed placed;
Vec3 vector;
"""


class NativeVec3ContextTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def setUp(self):
        super().setUp()
        self.cpp, self.cc = shutil.which("cpp"), shutil.which("gcc")
        if self.cpp is None or self.cc is None:
            self.skipTest("native cpp and gcc required")
        compiler = Compiler("gcc-2.8.1-sn64", "gnu", Path(self.cc), Path("as"), (), Path("sha256"))
        self.project = replace(
            self.project, compilers={compiler.id: compiler}, default_compiler=compiler.id, cppflags=("-P",)
        )
        self.contents = {}
        for name in ("types.h", "common/types_8a8189af7b05.h", "common/types_8fd754e1e915.h"):
            self.contents[self.project.include[0] / name] = (FIXTURE / name).read_bytes()
        self.consumer = self.project.include[0] / "aa-placed.h"
        self.wrapper = self.project.include[0] / "zz-provider.h"
        self.contents[self.consumer] = (FIXTURE / "placed.h").read_bytes()
        self.contents[self.wrapper] = b'#include "common/types_8fd754e1e915.h"\n'
        scalar = self.project.include[0] / "types.h"
        self.closures = {
            self.consumer: {self.consumer, scalar},
            self.wrapper: set(self.contents) - {self.consumer},
        }
        self.compiles = []

    def test_whole_provider_payload_digests(self):
        for name, record in json.loads((FIXTURE / "provenance.json").read_text()).items():
            data = (FIXTURE / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), record.get("slice_sha256", record["input_sha256"]))
            if record.get("complete"):
                self.assertEqual(len(data), record["bytes"])

    def native(self, expanded, version):
        compiled = subprocess.run(
            [self.cc, "-m32", "-std=gnu89", "-fsyntax-only", "-Werror=implicit-function-declaration", "-x", "c", "-"],
            input=expanded + CONSUMER,
            capture_output=True,
            text=True,
        )
        self.compiles.append(version)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertLess(expanded.index("struct Vec3 {"), expanded.index("struct Shared_Placed {"))
        return expanded

    def test_native_entry_points_compile_full_provider_and_consumer_in_all_holding_views(self):
        run = process.run_tool
        for version in self.versions:
            with self.subTest(version=version):
                job = database._Validation(
                    self.project,
                    SimpleNamespace(cpp=Path(self.cpp)),
                    version,
                    self.contents,
                    self.closures,
                    [],
                    frozenset(),
                )

                def compile_expanded(*args, holding=version, **kwargs):
                    return self.native(run(*args, **kwargs), holding)

                with (
                    patch.object(process, "run_tool", side_effect=compile_expanded) as native,
                    patch("unbake.decomp.draft_context.ordered_headers", wraps=ordered_headers) as order,
                ):
                    self.assertTrue(database._validate_version(job))
                self.assertEqual(native.call_count, 1)
                self.assertEqual(order.call_count, 1)
                self.assertEqual(order.call_args.kwargs["roots"], [self.consumer, self.wrapper])
        self.assertEqual(self.compiles, list(self.versions))
        self.assertFalse(list((self.project.build / "types").glob("held-header-context-*.c")))

    def test_missing_provider_still_refuses_and_preserves_the_real_context(self):
        contents = {path: self.contents[path] for path in self.closures[self.consumer]}
        job = database._Validation(
            self.project,
            SimpleNamespace(cpp=Path(self.cpp)),
            "de",
            contents,
            {self.consumer: set(contents)},
            [],
            frozenset(),
        )
        with self.assertRaises(Held) as caught:
            database._validate_version(job)
        self.assertEqual(caught.exception.key, "types.declaration")
        held = list((self.project.build / "types").glob("held-header-context-de-*.c"))
        self.assertEqual(len(held), 1)
        self.assertIn("struct Shared_Placed { Vec3 pos;", held[0].read_text())
