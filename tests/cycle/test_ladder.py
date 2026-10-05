"""The search ladder's own rules and TROUBLE.md."""

import unittest
from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase
from unbake.config import Held
from unbake.cycle import ladder
from unbake.search import BUILTINS


class LadderTests(unittest.TestCase):
    def test_methods_come_in_search_builtins_order(self) -> None:
        current = ladder.Ladder()
        seen = []
        while (method := current.next_method()) is not None:
            seen.append(method)
            current.tried[method] = 0.0
        self.assertEqual(tuple(seen), BUILTINS)

    def test_a_method_already_scored_or_skipped_is_not_run_again(self) -> None:
        self.assertEqual(ladder.Ladder(tried={"registers": 50.0}).next_method(), "order")
        self.assertEqual(ladder.Ladder(skipped={"registers": "n/a"}, tried={"order": 1.0}).next_method(), "permute")
        self.assertIsNone(ladder.Ladder(skipped={"registers": "n/a", "order": "n/a", "permute": "n/a"}).next_method())

    def test_only_a_strictly_higher_percent_is_a_gain(self) -> None:
        current = ladder.Ladder(best=60.0)
        self.assertTrue(current.gained(60.5))
        self.assertFalse(current.gained(60.0))
        self.assertFalse(current.gained(12.0))

    def test_the_snapshot_sits_beside_the_file_and_is_no_function(self) -> None:
        self.assertEqual(ladder.snapshot_path(Path("w/f/f.c")), Path("w/f/f.ladder.c"))


class TroubleTests(TempCase):
    def write(self, tried: dict, assembly: object = "glabel f\n nop\n", skipped: dict | None = None) -> str:
        file = self.root / "f.c"
        file.write_text("int f(void) { return 1; }\n")
        current = ladder.Ladder(tried=tried, best=72.5, skipped=skipped or {})
        target = patch.object(
            ladder, "target_assembly", side_effect=assembly if isinstance(assembly, Exception) else None
        )
        with target as fake:
            if not isinstance(assembly, Exception):
                fake.return_value = assembly
            path = ladder.write_trouble(object(), object(), "f", file, current, "first divergence: word 3")  # type: ignore[arg-type]
        self.assertEqual(path, self.root / "TROUBLE.md")
        return path.read_text()

    def test_it_holds_the_target_the_best_c_the_first_difference_and_each_method(self) -> None:
        text = self.write({"registers": 70.0, "order": 71.0}, skipped={"permute": "no mutation"})
        for part in (
            "Best compare so far: 72.50%",
            "glabel f",
            "int f(void) { return 1; }",
            "first divergence: word 3",
            "| registers | 70.00% |",
            "| order | 71.00% |",
            "| permute | skipped: no mutation |",
        ):
            self.assertIn(part, text)

    def test_missing_target_assembly_is_written_by_name_not_dropped(self) -> None:
        self.assertIn("unavailable: extract: no asm", self.write({}, Held("extract", "extract: no asm")))


if __name__ == "__main__":
    unittest.main()
