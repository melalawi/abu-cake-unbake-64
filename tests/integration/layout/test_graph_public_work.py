"""Existing public retention/extraction entries expose graph reuse and avoid unneeded AST clones."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import cache, cdecl, pool
from unbake.config import Held
from unbake.layout import header_loss
from unbake.typemap import declarations

FIXTURE = (TESTS / "layout/test_graph_public_work.py").parents[
    1
] / "typemap/fixtures/ragewars_vec3/include/common/types_8a8189af7b05.h"


class PublicGraphWork(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        cache.forget()
        cache.configure(memory_bytes=1 << 20)
        self.vec = self.project.include[0] / "common/vec.h"
        self.vec.parent.mkdir()
        self.payload = FIXTURE.read_bytes()
        self.vec.write_bytes(self.payload + b"\nextern int UnusedRemoved;\n")
        self.bridge = self.project.include[0] / "bridge.h"
        self.bridge.write_text('#include "common/vec.h"\n')
        for name in ("alpha", "beta"):
            (self.project.src / f"{name}.c").write_text(f'#include "bridge.h"\nvoid {name}(Vec3 *v) {{ (void)v; }}\n')
        (self.project.src / "gamma.c").write_text('#include "types.h"\nint gamma(void) { return 1; }\n')

    def counted(self, outputs):
        counts = {"source_rows": 0, "parent_parses": 0, "worker_parses": 0}
        worker = False
        real = cdecl.declarations

        def parsed(text):
            counts["worker_parses" if worker else "parent_parses"] += 1
            return real(text)

        def run(host, fn, jobs, shared=None):
            nonlocal worker
            worker = True
            try:
                if fn.__name__ == "_source_job":
                    counts["source_rows"] += sum(map(len, jobs))
                return [fn(job) if shared is None else fn(shared, job) for job in jobs]
            finally:
                worker = False

        with patch.object(pool, "run", run), patch.object(cdecl, "declarations", parsed):
            result = header_loss.check(self.project, outputs, policy=self.host)
        return result, counts

    def test_one_changed_real_header_revisits_exactly_two_consumers_then_zero(self):
        first, counts = self.counted({self.vec: self.payload})
        self.assertEqual(counts["source_rows"], 2)  # old public check schedules all three
        self.assertEqual(counts["parent_parses"], 0)
        self.assertGreater(counts["worker_parses"], 0)
        self.assertEqual(first.affected_consumers, tuple(self.project.src / f"{n}.c" for n in ("alpha", "beta")))
        second, repeated = self.counted({self.vec: self.payload})
        self.assertEqual(repeated, {"source_rows": 0, "parent_parses": 0, "worker_parses": 0})
        self.assertEqual(second.headers_checked, 0)
        self.assertEqual(second.closures_reused, 2)

    def test_include_only_rewiring_keeps_names_but_refuses_lost_vec3_reachability(self):
        with self.assertRaisesRegex(Held, "reachable declarations"):
            header_loss.check(self.project, {self.bridge: b'#include "types.h"\n'}, policy=None)

    def test_second_process_reuses_the_public_retention_certificate_without_parsing(self):
        header_loss.check(self.project, {self.vec: self.payload})
        script = """import json,sys
from pathlib import Path
from unittest.mock import patch
from unbake import cdecl,config
from unbake.layout import header_loss
project=config.load(Path(sys.argv[1]))
header=project.include[0]/"common/vec.h"
payload=Path(sys.argv[2]).read_bytes()
with patch.object(cdecl.NameParser,"parse",autospec=True,side_effect=cdecl.NameParser.parse) as parsed:
    header_loss.check(project,{header:payload})
print(json.dumps({"parses":parsed.call_count}))
"""
        environment = dict(os.environ, PYTHONPATH=str(Path(cdecl.__file__).parents[1]))
        ran = subprocess.run(
            [sys.executable, "-c", script, str(self.project.root), str(FIXTURE)],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(ran.stdout), {"parses": 0})  # old process-local cache reparses

    def test_real_declaration_ast_not_retained_needs_zero_cache_clones(self):
        from tests.fixtures import __path__ as fixture_paths

        fixture = Path(next(iter(fixture_paths))) / "ragewars_declaration_order"
        payload = (fixture / "scalar.h").read_text() + (fixture / "vec.h").read_text()
        cache.forget()
        cache.configure(memory_bytes=64)
        with patch.object(cache, "clone", wraps=cache.clone) as clones:
            facts = declarations.extract(
                payload, {"kind": "declared", "source": "declaration_evidence", "version": "us"}
            )
        self.assertEqual(clones.call_count, 0)  # old memo clones even its unobserved Future
        self.assertGreater(len(facts["structs"]), 0)
        self.assertEqual(cache.resident_bytes(), 0)
