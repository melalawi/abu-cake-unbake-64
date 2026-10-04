"""Publication between observations must not look like a corrupt cache entry."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.cache import Cache, key


class ParallelCacheTests(unittest.TestCase):
    def test_reader_accepts_publication_after_missing_stat(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Cache(Path(directory).resolve())
            identity = key("fixture")
            path = cache.path("cc", identity)
            path.parent.mkdir(parents=True)
            original = Path.stat

            def publish(candidate, *args, **kwargs):
                if candidate == path:
                    try:
                        return original(candidate, *args, **kwargs)
                    except FileNotFoundError:
                        candidate.write_bytes(b"object fixture")
                        raise
                return original(candidate, *args, **kwargs)

            with patch.object(Path, "stat", publish):
                self.assertIsNone(cache.get("cc", identity))
            self.assertEqual(cache.get("cc", identity).read_bytes(), b"object fixture")
            self.assertFalse(list(cache.root.rglob(".pending-*")))
