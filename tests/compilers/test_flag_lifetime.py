"""Repeated unit stages reuse registry parsing while changed flag contracts are read immediately."""

from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase
from unbake import cache
from unbake.compilers import drivers, registry
from unbake.config import Held


class FlagLifetimeTests(TempCase):
    def test_unit_stage_work_does_not_reparse_unchanged_registry(self):
        path = self.root / "registry.toml"
        source = registry.REGISTRY_PATH.read_text()
        path.write_text(source)
        cache.forget()
        real = Path.read_text
        reads = []

        def read(file, *args, **named):
            if file == path:
                reads.append(file)
            return real(file, *args, **named)

        with patch.object(registry, "REGISTRY_PATH", path), patch.object(Path, "read_text", read):
            stages = [drivers.stage_flags("sn64", ["-O2", "-funsigned-char"]) for _ in range(64)]
            before = len(reads)
            self.assertTrue(all(stage == stages[0] for stage in stages))
            self.assertIn("-funsigned-char", stages[0][0])
            path.write_text(source.replace('"-funsigned-char"', '"-fsigned-char"'))
            with self.assertRaisesRegex(Held, "unsupported"):
                drivers.stage_flags("sn64", ["-O2", "-funsigned-char"])
        self.assertEqual(before, 1)
