"""Real BT batch plus public header validation enforce one scoped sha256 certificate contract."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import cache
from unbake.config import Held
from unbake.typemap import database, regeneration

FIXTURES = Path(__file__).parent / "fixtures/consolidation_faults"


class RoutedCacheTests(ProjectCase):
    def test_real_incompatible_writer_batch_is_a_miss_and_recomputed_current_batch_is_a_hit(self):
        raw = (FIXTURES / "b1520_certificate_batch.json").read_bytes()
        self.assertEqual(
            (len(raw), hashlib.sha256(raw).hexdigest()),
            (17921, "bc7937f1bc28a1c35529aad1486a48e9907df797b7e798dc47fbe9224c1078a9"),
        )
        rows = json.loads(raw)
        self.assertEqual(len(rows), 256)
        store = cache.Cache(self.project.cache)
        certificates = store.certificates("typemap-certificates", "0" * 64)
        certificates.directory.mkdir(parents=True)
        (certificates.directory / "old.json").write_bytes(raw)
        wanted = [cache.key(*key.split(":")) for key in rows]
        with patch.object(store, "decode", wraps=store.decode) as decode:
            try:
                actual = certificates.contains(wanted)
            except Held as error:
                actual = {"held": error.key}
            self.assertEqual(actual, frozenset())
            self.assertEqual(decode.call_count, 1)
        certificates.add(wanted)
        self.assertEqual(certificates.contains(wanted), frozenset(wanted))
        self.assertTrue(all(len(value) == 64 for value in wanted))

    def test_public_writer_roundtrips_version_scoped_certificates(self):
        header = self.project.include[0] / "shared.h"
        # Native Vec3 fixture declaration, preserving its ABI shape.
        outputs = {header: b"typedef struct Vec3 { float x; float y; float z; } Vec3;\n"}
        jobs = []

        def prove(job):
            jobs.append(job)
            return cache.key(job.version, "context")

        with (
            patch.object(database, "_validate_version", side_effect=prove),
            patch("unbake.pool.run", side_effect=lambda host, fn, items: [fn(item) for item in items]),
        ):
            database.validate_headers(self.project, outputs, self.host)
        selected = [key for job in jobs for key in job.pending]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(len(key) == 64 for key in selected))
        certificates = cache.Cache(self.project.cache).certificates(
            "typemap-certificates", regeneration.environment(self.project, self.host)
        )
        self.assertEqual(certificates.contains(selected), frozenset(selected))
        self.assertNotEqual(selected[0], selected[1])
