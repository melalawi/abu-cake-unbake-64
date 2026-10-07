"""Fresh header publication verifies real fact receipts once; changed facts/proofs still require another pass."""

import copy
import json
from dataclasses import replace
from unittest.mock import patch

from tests.typemap.test_solve_reuse import SolveReuseFixture
from tests.typemap.test_types_work_counts import SLICE
from unbake import inputs, steps
from unbake.cache import Cache
from unbake.typemap import declarations, facts, solver, storage, types_db
from unbake.typemap.split import guarded


class PublishedInputConvergence(SolveReuseFixture):
    def setUp(self):
        super().setUp()
        payload = json.loads(SLICE.read_text())
        cache = Cache(self.project.cache)
        for digest, value in payload["shared"].items():
            cache.produce(facts.SHARED, digest, lambda target, value=value: target.write_text(json.dumps(value)))
        store = facts.Store(self.project, cache)
        self.seeds = [store.decode(row) for text in payload["rows"] for row in json.loads(text)]
        self.generated = self.project.include[0] / "common/types_fedcba987654.h"
        self.mode = "same"
        self.passes = 0
        self.collect.side_effect = self.collected
        functions = {name: row for seed in self.seeds for name, row in seed["functions"].items()}
        self.infer.return_value = {"constraints": [], "functions": functions}

    def collected(self, *args, **kwargs):
        seeds = copy.deepcopy(self.seeds)
        if self.generated.is_file():
            first = next(iter(seeds[0]["functions"].values()))
            if self.mode == "meaning":
                first["return"] = "float"
            elif self.mode == "receipt":
                first["provenance"]["sha256"] = "new proof stamp"
        return seeds

    def publish(self, project, result, previous, **kwargs):
        if not self.generated.is_file():
            self.generated.parent.mkdir(parents=True, exist_ok=True)
            self.generated.write_bytes(
                guarded(self.generated.relative_to(project.include[0]), "extern int learned(void);\n")
            )
        summary = {
            "functions": {
                name: {"semantic_sha256": storage.digest(storage.encoded(row)), "users": []}
                for name, row in result["functions"].items()
            }
        }
        staged, _ = types_db.stage(self.database, types_db.encode(result), summary, {})
        types_db.install(self.database, staged)

    def ensure(self):
        mapped = {"shard_sha256": "s", "shard": {}, "abi_supplement": None, "functions": {}, "globals": {}}
        original = steps.STEPS["types"]

        def run(project, host):
            self.passes += 1
            return original.run(project, host)

        step = replace(original, needs=(), run=run)
        with (
            patch.object(steps, "STEPS", {"types": step}),
            patch.object(solver, "refresh_map", return_value={}),
            patch("unbake.typemap.abi_facts.refine", return_value=mapped),
            patch.object(facts, "published_keys", side_effect=lambda *args: [inputs.digest(self.source)]),
            patch.object(declarations, "collect", self.collect),
            patch.object(solver, "_evidence", self.evidence),
            patch.object(solver, "infer", self.infer),
            patch("unbake.typemap.database.publish", self.published),
            patch("unbake.layout.header_step.missing", return_value=[]),
        ):
            result = steps.ensure(self.project, self.host, ["types"])
            self.assertEqual(steps.recorded(self.project, "types"), solver.input_key(self.project, self.host))
            return result

    def test_fresh_headers_with_identical_facts_and_receipts_need_one_types_pass(self):
        rows = self.ensure()
        self.assertEqual(self.passes, 1)
        self.assertEqual(self.infer.call_count, 1)
        self.assertEqual(self.published.call_count, 1)
        self.assertEqual(types_db.meta(self.database, "revision"), 1)
        self.assertEqual(sum(row.ran for row in rows), 1)
        self.ensure()
        self.assertEqual(self.passes, 1)

    def test_changed_meaning_requires_a_second_inference_and_publication(self):
        self.mode = "meaning"
        self.ensure()
        self.assertEqual(self.passes, 2)
        self.assertEqual(self.infer.call_count, 2)
        self.assertEqual(self.published.call_count, 2)
        self.assertEqual(types_db.meta(self.database, "revision"), 2)

    def test_changed_receipts_are_rebound_by_another_pass_even_when_meaning_stands(self):
        self.mode = "receipt"
        self.ensure()
        self.assertEqual(self.passes, 2)
        self.assertEqual(self.infer.call_count, 1)
        self.assertEqual(self.published.call_count, 2)
        self.assertEqual(types_db.meta(self.database, "revision"), 2)
