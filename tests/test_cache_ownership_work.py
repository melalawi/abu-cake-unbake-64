"""One retention owner using the native Vec3 and recorded RageWars fact payloads."""

import copy
import json
import multiprocessing
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import cache, inputs
from unbake.layout import header_loss
from unbake.typemap import inference_cache

VEC3 = Path(__file__).parent / "typemap/fixtures/ragewars_native_vec3/placed.h"
FACTS = Path(__file__).parent / "typemap/fixtures/ragewars_facts_slice.json"


def producer_process(root, seed, second, observed, release, start):
    """The second process announces its real pre-existing CAS miss before the first finishes."""
    original = cache.Cache._find

    def find(store, kind, key):
        result = original(store, kind, key)
        if second and kind == "types-inferred" and result is None:
            observed.set()
        return result

    if hasattr(cache, "configure"):
        cache.configure(memory_bytes=16 * 1024 * 1024)

    def compute():
        descriptor = os.open(root / "producers", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, b"producer\n")
        finally:
            os.close(descriptor)
        if not second:
            start.set()
            release.wait(10)
        return {"functions": copy.deepcopy(seed["functions"])}

    with patch.object(cache.Cache, "_find", find):
        inference_cache.infer(
            SimpleNamespace(root=root), cache.Cache(root / "cache"), ["ragewars-facts-slice"], [seed], compute
        )


class CacheOwnershipWork(TempCase):
    def setUp(self):
        super().setUp()
        if hasattr(cache, "configure"):
            cache.configure(memory_bytes=16 * 1024 * 1024)
        cache.forget()
        self.payload = VEC3.read_text()
        self.assertTrue(header_loss.declared(self.payload))
        cache.forget()

    def threaded(self, fail=False):
        source = self.root / "Vec3.h"
        source.write_text(self.payload)
        entered, release, second_lookup = threading.Event(), threading.Event(), threading.Event()
        lock = cache._memo_lock

        class ObservedLock:
            def __enter__(self):
                return lock.__enter__()

            def __exit__(self, *args):
                result = lock.__exit__(*args)
                if threading.current_thread().name == "second":
                    second_lookup.set()
                return result

        made, result, failures = [], [], []

        def compute():
            made.append(1)
            entered.set()
            release.wait(10)
            if fail:
                raise ValueError("real projection refused")
            return {"text": self.payload, "names": header_loss.declared(self.payload)}

        def call():
            try:
                result.append(cache.parsed("thread-vec3", source, compute))
            except ValueError as error:
                failures.append(str(error))

        threads = [threading.Thread(target=call, name=name) for name in ("first", "second")]
        with patch.object(cache, "_memo_lock", ObservedLock()):
            threads[0].start()
            self.assertTrue(entered.wait(10))
            threads[1].start()
            self.assertTrue(second_lookup.wait(10))
            release.set()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        return made, result, failures

    def test_two_threads_compute_one_real_projection(self):
        made, results, failures = self.threaded()
        self.assertEqual(len(made), 1)  # baseline duplicate producer, not a missing API
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["names"])
        results[0]["text"] = "poison"
        self.assertEqual(results[1]["text"], self.payload)

    def test_thread_failure_reaches_both_waiters_and_leaves_no_poisoned_entry(self):
        made, results, failures = self.threaded(fail=True)
        self.assertEqual(len(made), 1)
        self.assertEqual(results, [])
        self.assertEqual(failures, ["real projection refused"] * 2)
        made, results, failures = self.threaded()
        self.assertEqual(len(made), 1)
        self.assertEqual(len(results), 2)

    def test_mutation_cannot_poison_next_public_parsed_result(self):
        source = self.root / "Vec3.h"
        source.write_text(self.payload)
        made = []

        def compute():
            made.append(1)
            return {"text": self.payload, "names": header_loss.declared(self.payload)}

        first = cache.parsed("mutable-vec3", source, compute)
        first["text"] = "poison"
        second = cache.parsed("mutable-vec3", source, compute)
        self.assertEqual(second["text"], self.payload)
        self.assertEqual(len(made), 1)

    def test_two_processes_compute_one_real_fact_graph(self):
        row = json.loads(json.loads(FACTS.read_text())["rows"][0])[0]
        seed = {"functions": row["functions"], "globals": row["globals"], "aliases": {}}
        self.assertTrue(seed["functions"])
        ctx = multiprocessing.get_context("spawn")
        observed, release, start = ctx.Event(), ctx.Event(), ctx.Event()
        children = [
            ctx.Process(target=producer_process, args=(self.root, seed, i == 1, observed, release, start))
            for i in range(2)
        ]
        children[0].start()
        try:
            self.assertTrue(start.wait(10))
            children[1].start()
            self.assertTrue(observed.wait(10))
        finally:
            release.set()
            for child in children:
                if child.pid is not None:
                    child.join(10)
                    if child.is_alive():
                        child.terminate()
                        child.join()
        self.assertEqual([child.exitcode for child in children], [0, 0])
        self.assertEqual((self.root / "producers").read_text().splitlines(), ["producer"])

    def test_budget_bounds_retention_and_eviction_recomputes_real_payload(self):
        value = json.loads(FACTS.read_text())["rows"][0]
        budget = cache.memory_size(value) + cache.memory_size(("payload", "one"))
        cache.configure(memory_bytes=budget)
        made = []

        def compute():
            made.append(1)
            return value

        for key in ("one", "two", "one"):
            result = cache.memo("payload", key, compute, size=cache.memory_size, copy_out=str)
            self.assertEqual(result, value)
            self.assertLessEqual(cache.resident_bytes(), budget)
            self.assertGreater(cache.resident_bytes(), 0)
        self.assertEqual(len(made), 3)
        cache.configure(memory_bytes=16 * 1024 * 1024)

    def test_input_pin_tracks_negative_probe_and_symlink_spelling(self):
        root = self.root / "project"
        root.mkdir()
        source = root / "provider.h"
        missing = inputs.file_pin(source, root=root, root_id="project", reuse=True)
        self.assertEqual(missing.state, "missing")
        source.write_text(self.payload)
        present = inputs.file_pin(source, root=root, root_id="project", reuse=True)
        self.assertEqual(present.state, "file")
        self.assertNotEqual(missing, present)
        link = root / "shadow.h"
        link.symlink_to("provider.h")
        first = inputs.file_pin(link, root=root, root_id="project", reuse=True)
        link.unlink()
        link.symlink_to("./provider.h")
        self.assertNotEqual(first, inputs.file_pin(link, root=root, root_id="project", reuse=True))
        link.unlink()
        link.symlink_to(self.root / "outside.h")
        from unbake.config import Held

        with self.assertRaises(Held):
            inputs.file_pin(link, root=root, root_id="project", reuse=True)

    def test_every_header_recipe_dependency_invalidates_but_unrelated_cli_does_not(self):
        from tests.project_fixture import make
        from unbake.typemap import regeneration

        project, host = make(self.root)
        actual = inputs.digest
        paths = set()

        def capture(path, **kwargs):
            paths.add(Path(path))
            return actual(path, **kwargs)

        with patch.object(inputs, "digest", side_effect=capture):
            baseline = regeneration.environment(project, host)
        self.assertTrue(paths)
        self.assertFalse(any("cli" in path.parts or "tui" in path.parts for path in paths))
        for path in paths:

            def changed(candidate, changed_path=path, **kwargs):
                return "f" * 64 if Path(candidate) == changed_path else actual(candidate, **kwargs)

            with self.subTest(recipe=path.name), patch.object(inputs, "digest", side_effect=changed):
                self.assertNotEqual(regeneration.environment(project, host), baseline)

    def test_named_shared_payload_is_released_at_public_pool_completion(self):
        from tests.test_pool_shared import serial
        from unbake import pool

        shared = {"header": self.payload}
        workers = pool.Pool(2, 8 << 30, 1 << 30, 1 << 30, self.root / "transport")

        def consume(payload, index):
            return len(header_loss.declared(payload["header"])) + index

        original = pool.pickle.loads
        with (
            patch.object(pool.Pool, "_fresh"),
            patch.object(pool.Pool, "map", serial),
            patch.object(pool.pickle, "loads", wraps=original) as decoded,
        ):
            result = workers.run(consume, [0, 1, 2], shared)
        self.assertIsNone(pool._loaded)  # baseline retained the entire finished payload
        self.assertEqual(decoded.call_count, 1)
        self.assertEqual(len(result), 3)
        self.assertTrue(all(result))
        self.assertEqual(list((self.root / "transport").iterdir()), [])

    def test_certificates_are_batched_reused_and_disposable_in_the_cache_owner(self):
        from unbake import atomic

        store = cache.Cache(self.root / "certs")
        certificates = store.certificates("headers", cache.key("environment"))
        keys = [cache.key("one"), cache.key("two")]
        with patch.object(atomic, "write", wraps=atomic.write) as writes:
            certificates.add(keys)
        self.assertEqual(writes.call_count, 1)
        self.assertFalse(writes.call_args.kwargs["durable"])
        original = cache.JsonCodec.decode
        with patch.object(cache.JsonCodec, "decode", autospec=True, side_effect=original) as decoded:
            self.assertEqual(certificates.contains(keys), frozenset(keys))
            self.assertEqual(certificates.contains(keys), frozenset(keys))
        self.assertEqual(decoded.call_count, 1)
        self.assertEqual(len(cache.entries(store.root)), 1)
        self.assertEqual(len(cache.trim(store.root, 1, 0)), 1)
        self.assertEqual(certificates.contains(keys), frozenset())

    def test_public_resolvers_reuse_one_real_alias_normalization(self):
        from unbake.typemap import declarations, header_names

        scalar = (VEC3.parent / "types.h").read_text()
        aliases = header_names.alias_types(scalar)
        self.assertEqual(aliases["s32"], "signed int")
        with patch.object(declarations, "canonical", wraps=declarations.canonical) as produced:
            first = declarations.resolver(aliases)
            self.assertEqual(first("s32"), "int")
            second = declarations.resolver(dict(aliases))
            self.assertEqual(second("s32"), "int")
            self.assertEqual(produced.call_count, 1)
            self.assertEqual(second("s32"), "int")
            self.assertEqual(produced.call_count, 1)
            aliases["s32"] = "float"
            self.assertEqual(first("s32"), "int")
            self.assertEqual(declarations.resolver(aliases)("s32"), "float")
            self.assertEqual(produced.call_count, 2)
