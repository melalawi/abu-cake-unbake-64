"""Real ROM words bound refresh work by changed intervals and dependency neighbours."""

from contextlib import contextmanager
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import pool
from unbake.typemap import mapping, shards


class RefreshCounts(ProjectCase):
    versions = ("us",)

    @contextmanager
    def counted_pool(self):
        state = {"worker": False, "jobs": [], "parent_serial": 0, "reads": []}
        real_pack, real_version = shards.pack, shards.Functions.version

        def run(host, fn, items, shared=None):
            state["jobs"].append(len(items))
            state["worker"] = True
            try:
                return [fn(item) if shared is None else fn(shared, item) for item in items]
            finally:
                state["worker"] = False

        def pack(value):
            state["parent_serial"] += int(not state["worker"])
            return real_pack(value)

        def version(functions, name, version):
            state["reads"].append(name)
            state["parent_serial"] += int(not state["worker"])
            return real_version(functions, name, version)

        with (
            patch.object(pool, "run", run),
            patch.object(shards, "pack", pack),
            patch.object(shards.Functions, "version", version),
        ):
            yield state

    def test_full_map_has_zero_parent_body_jobs_and_spreads_function_jobs(self):
        with self.counted_pool() as counts:
            result = mapping.map_program(self.project, self.host)
        self.assertEqual(counts["parent_serial"], 0)
        self.assertEqual(sum(counts["jobs"]), 3)
        self.assertEqual(len(result["functions"]), 3)

    def test_one_landing_does_not_read_unaffected_assembly_facts(self):
        with self.counted_pool():
            mapping.map_program(self.project, self.host)
        split = self.project.version("us").split
        split.write_text(split.read_text().replace("asm, alpha", "c, alpha"))
        with self.counted_pool() as counts:
            result = mapping.refresh_map(self.project, self.host)
        self.assertEqual(counts["reads"], [])  # the ownership overlay needs no instruction bodies
        self.assertEqual(counts["parent_serial"], 0)
        self.assertEqual(result["refresh"]["rescanned"], 0)
        self.assertEqual(result["functions"]["alpha"]["versions"]["us"]["kind"], "c")

    def test_new_symbol_reads_only_its_real_memory_neighbour(self):
        # alpha loads through a constant address; beta and gamma never touch it.
        from tests.project_fixture import make

        self.project, self.host = make(self.root / "memory", [0x3C088000, 0x8D023000, 0x03E00008, 0], self.versions)
        with self.counted_pool():
            mapping.map_program(self.project, self.host)
        symbols = self.project.version("us").symbols
        symbols.write_text(symbols.read_text() + "landed_data = 0x80003000;\n")
        with self.counted_pool() as counts:
            result = mapping.refresh_map(self.project, self.host)
        self.assertLessEqual(set(counts["reads"]), {"alpha"})
        self.assertEqual(counts["parent_serial"], 0)
        self.assertEqual(result["refresh"]["rescanned"], 1)
        self.assertEqual(result["globals"]["landed_data"]["accesses"][0]["function"], "alpha")


@pool.cpu
def worker_analysis(job):
    """Actual payload with a startup barrier so job distribution can be counted reliably."""
    import os
    import time
    from pathlib import Path

    directory, barrier, index = job
    Path(directory, str(os.getpid())).touch()
    deadline = time.monotonic() + 10
    while len(list(Path(directory).iterdir())) < barrier:
        if time.monotonic() > deadline:
            raise RuntimeError("not every admitted worker received a job")
        time.sleep(0.01)
    result = mapping._analysis(
        {"us": ({}, {})}, ("alpha", "us", 0x80001000, 0, bytes.fromhex("2402000103e0000800000000"))
    )
    return os.getpid(), index, result["register_outputs"]


class PhysicalWorkerCounts(ProjectCase):
    versions = ("us",)

    def physical_host(self):
        from unbake.config import Host

        values = {name: dict(row) for name, row in self.host.values.items()}
        values["resources"].update(
            cores=4,
            workers=4,
            memory_total_bytes=4_000_000_000,
            memory_parent_bytes=1_000_000_000,
            memory_worker_bytes=512_000_000,
        )
        return Host.from_values(values, "draft")

    def test_one_body_job_is_never_run_in_the_parent(self):
        import os

        host = self.physical_host()
        directory = self.root / "workers"
        directory.mkdir()
        [(pid, index, registers)] = pool.run(host, worker_analysis, [(str(directory), 1, 0)])
        self.assertNotEqual(pid, os.getpid())
        self.assertEqual(index, 0)
        self.assertIn("r2", registers)

    def test_actual_payload_jobs_use_every_admitted_worker(self):
        from collections import Counter

        directory = self.root / "workers"
        directory.mkdir()
        host = self.physical_host()
        admitted = pool.workers(host)
        jobs = [(str(directory), admitted, index) for index in range(16)]
        results = pool.run(host, worker_analysis, jobs)
        counts = Counter(pid for pid, _, _ in results)
        self.assertEqual(len(counts), admitted)
        self.assertEqual(sum(counts.values()), 16)
        self.assertTrue(all(count > 0 for count in counts.values()))
