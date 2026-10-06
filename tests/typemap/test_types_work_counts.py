"""The types step's repeated work is bounded by what changed, counted on real RageWars fact payloads."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.kit import TempCase
from unbake.cache import Cache
from unbake.typemap import facts, inference_cache, layers

SLICE = Path(__file__).parent / "fixtures" / "ragewars_facts_slice.json"


class RealSlice(TempCase):
    """Twenty real encoded per-source fact rows and the shared values they name, in a fresh cache."""

    def setUp(self):
        super().setUp()
        payload = json.loads(SLICE.read_text())
        self.project = SimpleNamespace(root=self.root)
        self.cache = Cache(self.root / "cache")
        self.rows = payload["rows"]
        for digest, value in payload["shared"].items():
            data = json.dumps(value, separators=(",", ":")).encode()
            self.cache.produce(facts.SHARED, digest, lambda target, data=data: target.write_bytes(data))

    def seeds(self, store, stamp=None):
        """Decoded seeds of every row, optionally re-stamped as if the sources' bytes changed."""
        result = []
        for text in self.rows:
            row = text if stamp is None else text.replace('"sha256":"', '"sha256":"' + stamp)
            result.extend(store.decode(entry) for entry in json.loads(row))
        return result


class InferenceReuseCounts(RealSlice):
    def graph(self, seeds):
        return {"functions": [seed["functions"] for seed in seeds], "constraints": []}

    def test_a_fact_neutral_landing_rebinds_receipts_without_walking_the_graph(self):
        store = facts.Store(self.project, self.cache)
        first = self.seeds(store)
        compute = MagicMock(side_effect=lambda: self.graph(first))
        walks = []
        original = inference_cache.Receipts._transform

        def spy(receipts, *args, **named):
            walks.append(1)
            return original(receipts, *args, **named)

        with patch.object(inference_cache.Receipts, "_transform", spy):
            inference_cache.infer(self.project, self.cache, ["map"], first, compute, output=store)
            cold = len(walks)
            warm, _, _ = inference_cache.infer(
                self.project, self.cache, ["map"], self.seeds(store, "x"), compute, output=store
            )
        self.assertEqual(compute.call_count, 1)
        self.assertEqual((cold, len(walks) - cold), (1, 0))
        self.assertTrue(all(row["sha256"].startswith("x") for row in self._receipts(warm)))

    def _receipts(self, graph):
        for functions in graph["functions"]:
            for row in functions.values():
                yield row["provenance"]

    def test_decoded_shared_values_are_not_hashed_again_when_a_solve_keys_its_seeds(self):
        store = facts.Store(self.project, self.cache)
        seeds = self.seeds(store)
        with patch("json.dumps", side_effect=AssertionError("shared value serialized again")):
            for seed in seeds:
                store.encode(seed)


class SpellingCounts(TempCase):
    def test_a_path_is_spelled_once_however_many_keys_name_it(self):
        project = SimpleNamespace(root=self.root)
        layers._spell_in.cache_clear()
        header = str(self.root / "include" / "types.h")
        for _ in range(100):
            layers.spelling(project, self.root / "machine", header)
        self.assertEqual(layers._spell_in.cache_info().misses, 1)
