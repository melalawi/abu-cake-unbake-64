"""Real RW ROM bodies expose empty/partial analyzer-upgrade publication and bounded recovery."""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.project_fixture import make
from unbake import cache, config, pool, steps
from unbake.cli import recompute
from unbake.config import Held
from unbake.typemap import mapping, shards, storage

VERSIONS = ("de", "eu", "eu-x", "us", "us-rev1")
NAMES = ("func_802651B0_de", "__cmpdi2")
FIXTURES = Path(__file__).parents[1] / "fixtures/map_integrity"


class IntegrityTests(TempCase):
    def setUp(self):
        super().setUp()
        cache.forget()
        self.project, self.host = make(self.root, versions=VERSIONS)
        configuration = self.project.root / "config.toml"
        text = configuration.read_text()
        for version in VERSIONS:
            configured = self.project.version(version)
            bodies = [(FIXTURES / f"{name}-{version}.bin").read_bytes() for name in NAMES]
            start, middle, end = 0x40, 0x40 + len(bodies[0]), 0x40 + sum(map(len, bodies))
            configured.split.write_text(
                "segments:\n  - [0x0, header, header]\n  - name: main\n    type: code\n"
                "    start: 0x40\n    vram: 0x802651B0\n    subalign: 4\n    subsegments:\n"
                f"      - [0x{start:X}, asm, {NAMES[0]}]\n      - [0x{middle:X}, asm, {NAMES[1]}]\n"
                f"  - [0x{end:X}]\n"
            )
            configured.symbols.write_text(
                f"{NAMES[0]} = 0x802651B0;\n{NAMES[1]} = 0x{0x802651B0 + len(bodies[0]):X};\n"
            )
            configured.baserom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + b"".join(bodies))
            digest = hashlib.sha1(configured.baserom.read_bytes()).hexdigest()
            text = re.sub(
                rf'(\[version\.{re.escape(version)}\]\n[^\[]*?baserom_sha1 = ")[^"]+',
                lambda match, digest=digest: match[1] + digest,
                text,
            )
        configuration.write_text(text)
        self.project = config.load(self.project.root)

    @contextmanager
    def work(self):
        counts = {"mapped": 0, "packed": 0, "copied": 0}
        real_pack, real_copy = shards.pack, shards.Writer.copy

        def run(host, function, items, shared=None):
            jobs = list(items)
            if function is mapping._mapped:
                counts["mapped"] += len(jobs)
            return [function(item) if shared is None else function(shared, item) for item in jobs]

        def pack(body):
            counts["packed"] += 1
            return real_pack(body)

        def copy(writer, source, owners):
            counts["copied"] += len(owners)
            return real_copy(writer, source, owners)

        with (
            patch.object(pool, "run", run),
            patch.object(shards, "pack", pack),
            patch.object(shards.Writer, "copy", copy),
        ):
            yield counts

    def manifest(self):
        return self.project.build / "map/facts.json"

    def pairs(self, facts):
        with sqlite3.connect(f"file:{self.manifest().parent / facts['shard']}?mode=ro", uri=True) as reader:
            return set(reader.execute("SELECT name,version FROM functions"))

    def expected(self):
        return {(name, version) for name in NAMES for version in VERSIONS}

    def seed(self):
        with self.work():
            mapping.map_program(self.project, self.host)

    def obsolete(self):
        facts = json.loads(self.manifest().read_bytes())
        facts["abi_analysis_sha256"] = "0" * 64
        self.manifest().write_bytes(storage.encoded(facts))

    def test_analyzer_upgrade_keeps_every_unchanged_real_body(self):
        self.seed()
        self.obsolete()
        # Ownership changes make the old public refresh enter its retained-body branch.
        split = self.project.version("de").split
        split.write_text(split.read_text().replace("asm,", "c,"))
        with self.work() as counts:
            facts = mapping.refresh_map(self.project, self.host)
        self.assertEqual(self.pairs(facts), self.expected())  # baseline publishes ZERO rows
        self.assertEqual(counts, {"mapped": 10, "packed": 10, "copied": 0})
        self.assertEqual(facts["refresh"]["rescanned"], 10)
        with self.work() as unchanged:
            again = mapping.refresh_map(self.project, self.host)
        self.assertEqual(unchanged, {"mapped": 0, "packed": 0, "copied": 0})
        self.assertEqual(again["shard"], facts["shard"])

    def test_analyzer_upgrade_mixed_changed_and_retained_intervals_is_complete(self):
        self.seed()
        self.obsolete()
        # Only de's two adjacent intervals change; eight bodies remain byte-identical.
        split = self.project.version("de").split
        old = 0x40 + len((FIXTURES / f"{NAMES[0]}-de.bin").read_bytes())
        split.write_text(split.read_text().replace(f"0x{old:X}, asm", f"0x{old + 4:X}, asm"))
        with self.work() as counts:
            facts = mapping.refresh_map(self.project, self.host)
        self.assertEqual(self.pairs(facts), self.expected())  # baseline publishes only changed rows
        self.assertEqual(counts, {"mapped": 10, "packed": 10, "copied": 0})

    def damage(self, kept):
        facts = json.loads(self.manifest().read_bytes())
        path = self.manifest().parent / f"private-corruption-{kept}.sqlite"
        with sqlite3.connect(path) as writer, sqlite3.connect(self.manifest().parent / facts["shard"]) as reader:
            writer.execute("CREATE TABLE functions(name TEXT,version TEXT,body BLOB,PRIMARY KEY(name,version))")
            writer.executemany(
                "INSERT INTO functions VALUES (?,?,?)", list(reader.execute("SELECT * FROM functions"))[:kept]
            )
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        destination = path.with_name(f"facts-{digest}.sqlite")
        destination.write_bytes(content)
        facts.update(shard=destination.name, shard_sha256=digest)
        self.manifest().write_bytes(storage.encoded(facts))

    def test_reader_refuses_digest_valid_empty_and_partial_before_decompression(self):
        self.seed()
        original = self.manifest().read_bytes()
        for kept in (0, 3):
            with self.subTest(kept=kept):
                self.manifest().write_bytes(original)
                self.damage(kept)
                with (
                    patch.object(shards.zlib, "decompress", wraps=shards.zlib.decompress) as decoded,
                    self.assertRaisesRegex(Held, "map.shards.inventory") as held,
                ):
                    mapping.load_map(self.project)
                self.assertEqual(decoded.call_count, 0)
                self.assertEqual(held.exception.data["expected_rows"], 10)
                self.assertEqual(held.exception.data["actual_rows"], kept)

    def test_public_refresh_recovers_empty_and_partial_from_confirmed_rom(self):
        self.seed()
        original = self.manifest().read_bytes()
        for kept in (0, 3):
            with self.subTest(kept=kept):
                self.manifest().write_bytes(original)
                self.damage(kept)
                with self.work() as counts:
                    facts = mapping.refresh_map(self.project, self.host)
                self.assertEqual(self.pairs(facts), self.expected())
                self.assertEqual(counts, {"mapped": 10, "packed": 10, "copied": 0})

    def test_public_rom_facts_only_recovery_skips_every_dependency_and_native_job(self):
        self.seed()
        self.damage(3)
        parser = argparse.ArgumentParser()
        recompute.register(parser)
        args = parser.parse_args(["rom-facts", "--rom-facts-only"])
        context = SimpleNamespace(
            args=args, project=lambda: self.project, require_host=lambda: self.host, cmd=lambda *words: "unbake next"
        )
        with self.work() as counts, patch.object(steps, "recompute") as dependencies:
            result = recompute.run(context)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["rows"], 10)
        self.assertEqual(dependencies.call_count, 0)
        self.assertEqual(counts, {"mapped": 10, "packed": 10, "copied": 0})
        self.assertEqual(self.pairs(result.data), self.expected())

    def test_producer_refuses_incomplete_inventory_before_publication(self):
        writer = shards.Writer(self.root)
        try:
            writer.add("__cmpdi2", "de", {"actual_payload": (FIXTURES / "__cmpdi2-de.bin").read_bytes().hex()})
            with self.assertRaisesRegex(Held, "map.shards.inventory"):
                writer.finish([("__cmpdi2", "de"), ("__cmpdi2", "us")])
            self.assertEqual(list(self.root.glob("facts-*.sqlite")), [])
        finally:
            writer.close()
