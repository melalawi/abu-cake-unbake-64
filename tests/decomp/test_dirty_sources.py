"""checks.dirty: the sources with unmarked findings, rescanned only when a source's stat signature changes."""

from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase
from unbake import pool
from unbake.cache import Cache
from unbake.decomp import checks


def serial(host: object, fn: object, items: list[object]) -> list[object]:
    return [fn(item) for item in items]  # type: ignore[operator]


BROKEN = "struct func_80000000_S1 {\n    int a;\n};\n"
CLEAN = "int alpha(void) { return 0; }\n"


class DirtySourcesTests(TempCase):
    def test_scan_is_reused_until_a_source_changes(self) -> None:
        cache = Cache(self.root / "cache")
        clean, broken = self.root / "clean.c", self.root / "broken.c"
        clean.write_text(CLEAN)
        broken.write_text(BROKEN)
        sources = [broken, clean]
        with (
            patch.object(checks, "unmarked", wraps=checks.unmarked) as scanned,
            patch.object(pool, "run", side_effect=serial) as ran,
        ):
            self.assertEqual(checks.dirty(cache, sources, None), [broken])
            self.assertEqual(checks.dirty(cache, sources, None), [broken])
            self.assertEqual(scanned.call_count, 2)
            broken.write_text(CLEAN + "\n")
            self.assertEqual(checks.dirty(cache, sources, None), [])
            self.assertEqual(scanned.call_count, 4)
            self.assertEqual(ran.call_count, 2)  # every rescan goes through the pool
        self.assertEqual(checks.dirty(cache, [Path(clean)], None), [])
