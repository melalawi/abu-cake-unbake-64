"""Per-source facts: content keys, shared interning and isolation between sources."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.cache import Cache
from unbake.typemap import declarations, facts


class FactsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project, self.policy, _ = fixture(self.root, versions=("us", "eu"), case=self)
        include = self.project.include[0]
        (include / "used.h").write_text('#include "nested.h"\ntypedef int Used;\n')
        (include / "nested.h").write_text("typedef int Nested;\n")
        (include / "unrelated.h").write_text("typedef int Unrelated;\n")
        self.source = self.project.src / "alpha.c"
        self.source.write_text('#include "used.h"\nint alpha(void) { return 1; }\n')
        self.task = ("alpha", self.source, "us")

    def key(self) -> str:
        return facts.source_key(self.project, None, self.task)

    def test_key_follows_the_include_closure_only(self) -> None:
        before = self.key()
        (self.project.include[0] / "unrelated.h").write_text("typedef long Unrelated;\n")
        self.assertEqual(self.key(), before)
        (self.project.include[0] / "nested.h").write_text("typedef long Nested;\n")
        changed = self.key()
        self.assertNotEqual(changed, before)
        self.source.write_text(self.source.read_text() + "/* comment */\n")
        self.assertNotEqual(self.key(), changed)

    def test_key_names_the_version(self) -> None:
        self.assertNotEqual(self.key(), facts.source_key(self.project, None, ("alpha", self.source, "eu")))

    def test_store_interns_shared_values_by_content(self) -> None:
        cache = Cache(self.root / "cache")
        template = {"Shape": {"type": "struct Shape", "size": 4, "fields": []}}
        aliases = {"Word": "int"}
        seeds = [
            {"structs": declarations.ProvenStructs(template, {"function": name}), "aliases": aliases, "unknown": []}
            for name in ("alpha", "beta")
        ]
        facts.Store(cache).put("a" * 64, seeds)
        loaded = facts.Store(cache).get("a" * 64)
        assert loaded is not None
        first, second = loaded
        self.assertIs(first["structs"].template, second["structs"].template)
        self.assertIs(first["aliases"], second["aliases"])
        self.assertEqual(second["structs"]["Shape"]["provenance"], {"function": "beta"})
        self.assertEqual(len(list((self.root / "cache" / facts.SHARED).rglob("*"))) - 2, 2)

    def test_refresh_reports_each_refused_function_and_caches_the_rest(self) -> None:
        bad = self.project.src / "bad.c"
        bad.write_text("Missing bad;\nint bad(void) { return 1; }\n")
        with patch.object(self.policy, "cache_root", self.root / "cache"):
            refused = facts.refresh(self.project, self.policy, [("bad", bad, "us"), self.task])
            self.assertEqual(set(refused), {"bad"})
            self.assertTrue(facts.store(self.policy).has(facts.source_key(self.project, self.policy, self.task)))


if __name__ == "__main__":
    unittest.main()
