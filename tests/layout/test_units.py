import tempfile
import unittest
from pathlib import Path

from unbake.layout import units
from unbake.project_tools import extract


def rows(*items: str) -> str:
    return "".join(f"      - [{0x100 + 4 * i:#x}, {item}]\n" for i, item in enumerate(items))


class UnitTests(unittest.TestCase):
    def test_pool_runs(self) -> None:
        table = [
            (['rodata, "rodata/f/1"', 'rodata, "rodata/f/2"', 'rodata, "rodata/f/3"'], [0]),
            (['rodata, "rodata/f/1"', 'rodata, "rodata/g/2"'], [0, 1]),
            (['rodata, "rodata/f/1"', 'asm, "f"', 'rodata, "rodata/f/3"'], [0, 1, 2]),
            (['data, "rodata/f/1"', 'rodata, "rodata/f/2"'], [0, 1]),
            (['data, "rodata/shared/1"', 'data, "rodata/shared/2"'], [0, 1]),
            (['rodata, "rodata/unresolved/1"', 'rodata, "rodata/unresolved/2"'], [0, 1]),
            (['rodata, "rodata/f/1"', 'rodata, "rodata/f/2", { align: 8 }'], [0, 1]),
            (['c, "f"', 'c, "g"'], [0, 1]),
        ]
        for items, kept in table:
            with self.subTest(items=items):
                source = rows(*items)
                lines = source.splitlines(keepends=True)
                self.assertEqual(units.merge_pools(source), "".join(lines[i] for i in kept))

    def test_prune_removes_only_matched_and_pool_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for entry in ("f.s", "g.s", "kept.s", "data/rodata/f/1.rodata.s", "data/rodata/f/2.rodata.s", "assets/x.s"):
                (root / entry).parent.mkdir(parents=True, exist_ok=True)
                (root / entry).write_text("")
            text = rows('c, "f"', 'asm, "g"', 'rodata, "rodata/f/1"')
            removed = extract.prune_stale(root, text, {(root / "data/rodata/f/1.rodata.s").resolve()})
            self.assertEqual(removed, 2)
            left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.s"))
            self.assertEqual(left, ["assets/x.s", "data/rodata/f/1.rodata.s", "g.s", "kept.s"])


if __name__ == "__main__":
    unittest.main()
