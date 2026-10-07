"""Two naturally batched RW header payloads under the actual 512MB worker cap."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures/consolidation_faults"
WORKER = """
import gzip, hashlib, json, pathlib, resource, sys
from unbake import cache
from unbake.typemap import declarations
resource.setrlimit(resource.RLIMIT_DATA, (512000000, 512000000))
cache.configure(memory_bytes=2000000000)
context = json.loads(gzip.decompress(pathlib.Path(sys.argv[1]).read_bytes()))
authored = {pathlib.Path(name) for name in context["authored_headers"]}
ast_requests = 0
ast_retained_bytes = 0
real_remember = cache.remember
def remember(kind, content, value, **kwargs):
    global ast_requests, ast_retained_bytes
    before = cache.resident_bytes()
    result = real_remember(kind, content, value, **kwargs)
    if kind == "decl.tree":
        ast_requests += 1
        ast_retained_bytes += cache.resident_bytes() - before
    return result
cache.remember = remember
results = []
for name in sys.argv[2:]:
    text = gzip.decompress(pathlib.Path(name).read_bytes()).decode()
    version = "us" if "_us." in name else "eu"
    seed = declarations.extract(
        text,
        {"kind": "declared", "version": version, "sha256": hashlib.sha256(text.encode()).hexdigest()},
        authored_headers=authored,
    )
    results.append({
        "version": version, "functions": len(seed["functions"]),
        "structs": len(seed["structs"]), "input_bytes": len(text.encode()),
    })
print(json.dumps({
    "results": results, "retained_bytes": cache.resident_bytes(),
    "worker_cap_bytes": resource.getrlimit(resource.RLIMIT_DATA)[0],
    "ast_requests": ast_requests, "ast_retained_bytes": ast_retained_bytes,
}))
"""


class RealWorkerTests(unittest.TestCase):
    def test_actual_rw_headers_replay_in_one_512mb_worker_without_a_whole_solve(self):
        paths = [FIXTURES / ("rw_headers_" + v + ".c.gz") for v in ("us", "eu")]
        self.assertTrue(all(path.is_file() for path in paths), "real routed header payloads required")
        expected = json.loads((FIXTURES / "provenance.json").read_text())["headers"]["versions"]
        completed = subprocess.run(
            [sys.executable, "-c", WORKER, str(FIXTURES / "rw_headers_context.json.gz"), *map(str, paths)],
            text=True,
            capture_output=True,
            env=dict(os.environ, PYTHONPATH=str(Path.cwd() / "src")),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
        result = json.loads(completed.stdout)
        self.assertEqual(result["worker_cap_bytes"], 512000000)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual((result["ast_requests"], result["ast_retained_bytes"]), (2, 0))
        for row in result["results"]:
            self.assertEqual(row["input_bytes"], expected[row["version"]]["bytes"])
            self.assertGreater(row["functions"], 0)
            self.assertGreater(row["structs"], 0)
        self.assertLessEqual(result["retained_bytes"], 2000000000)
