"""Map bodies read one by one share one read-only connection per thread; a pickled map opens its own."""

import pickle
import sqlite3
from unittest.mock import patch

from tests.kit import TempCase
from unbake.typemap import shards


class ConnectionTests(TempCase):
    def test_one_connection_serves_every_read_and_a_copy_reads_alike(self) -> None:
        writer = shards.Writer(self.root)
        for name in ("a", "b"):
            writer.add(name, "us", {"memory": [name]})
        path = writer.finish()
        functions = shards.Functions(path, {name: {"versions": {"us": {"address": 1}}} for name in ("a", "b")})
        real, opened = sqlite3.connect, []

        def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
            opened.append(args)
            return real(*args, **kwargs)

        with patch.object(shards.sqlite3, "connect", connect):
            bodies = [functions.version(name, "us") for name in ("a", "b", "a")]
            copy = pickle.loads(pickle.dumps(functions))
            copied = copy.version("b", "us")
        self.assertEqual([body["memory"] for body in bodies], [["a"], ["b"], ["a"]])
        self.assertEqual(copied, {"memory": ["b"], "address": 1})
        self.assertEqual(len(opened), 2)  # one for the original, one for the copy
