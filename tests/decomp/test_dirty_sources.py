"""Complete per-content findings replace the whole-list dirty-path store."""

from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake.cache import Cache
from unbake.decomp import checks

BROKEN = "struct func_80000000_S1 {\n    int a;\n};\n"
CLEAN = "int alpha(void) { return 0; }\n"


class DirtySourcesTests(TempCase):
    def test_scan_is_reused_per_source_and_invalidates_only_changed_content(self):
        cache = Cache(self.root / "cache")
        project = SimpleNamespace(root=self.root)
        clean, broken = self.root / "clean.c", self.root / "broken.c"
        clean.write_text(CLEAN)
        broken.write_text(BROKEN)
        sources = [broken, clean]
        with patch.object(checks, "run", wraps=checks.run) as scanned:
            result = checks.findings(project, sources, cache)
            self.assertEqual({r.path for r in result.unmarked}, {"broken.c"})
            self.assertEqual(result.source_scans, 2)
            self.assertEqual(checks.findings(project, sources, cache).source_scans, 0)
            self.assertEqual(scanned.call_count, 2)
            broken.write_text(CLEAN + "\n")
            changed = checks.findings(project, sources, cache)
            self.assertEqual(changed.unmarked, ())
            self.assertEqual(changed.source_scans, 1)
            self.assertEqual(scanned.call_count, 3)

    def test_mutating_one_result_cannot_poison_the_cached_findings(self):
        source = self.root / "source.c"
        source.write_text("/* FAKEMATCH: documented fixture */\n" + '#include "../a.h"\n')
        project = SimpleNamespace(root=self.root)
        cache = Cache(self.root / "cache")
        first = checks.findings(project, (source,), cache)
        self.assertTrue(first.rows)
        self.assertEqual(first.unmarked, ())
        first.dependency_hashes.clear()
        second = checks.findings(project, (source,), cache)
        self.assertTrue(second.dependency_hashes)
        self.assertEqual(first.rows, second.rows)
        self.assertEqual(second.source_scans, 0)
