"""Trial entry proof for functions reached only through pointers."""

import unittest
from pathlib import Path

from unbake.decomp.trial_link import Elf, Section, Symbol, generation_entry
from unbake.project.config import Held


def layout(*symbols: Symbol) -> Elf:
    text = Section("5", ".text", "PROGBITS", 0x80400000, 0x1000, 0x400, "AX")
    return Elf(Path("generation.elf"), {"5": text}, list(symbols), {})


class GenerationEntryTests(unittest.TestCase):
    def test_named_entry_must_sit_at_the_split_address(self) -> None:
        named = Symbol("func_80400100", 0x80400100, 16, "FUNC", "GLOBAL", "5")
        self.assertEqual(generation_entry(layout(named), "func_80400100", 0x80400100), named)
        with self.assertRaisesRegex(Held, "disagrees"):
            generation_entry(layout(named), "func_80400100", 0x80400200)

    def test_unnamed_pointer_entry_is_found_by_address(self) -> None:
        # The extractor names an unreferenced function after its address until a draft names it.
        automatic = Symbol("func_80400100_auto", 0x80400100, 16, "FUNC", "GLOBAL", "5")
        data = Symbol("func_80400100_auto.NON_MATCHING", 0x80400100, 16, "OBJECT", "GLOBAL", "5")
        self.assertEqual(generation_entry(layout(automatic, data), "func_80400100", 0x80400100), automatic)
        with self.assertRaisesRegex(Held, "entry at 0x80400200"):
            generation_entry(layout(automatic), "func_80400100", 0x80400200)
        twin = Symbol("other_alias", 0x80400100, 16, "FUNC", "GLOBAL", "5")
        with self.assertRaisesRegex(Held, "entry at 0x80400100"):
            generation_entry(layout(automatic, twin), "func_80400100", 0x80400100)


if __name__ == "__main__":
    unittest.main()
