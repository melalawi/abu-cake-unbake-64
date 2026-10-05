"""Each worker entry module imports first in a clean module table: a worker starts that way, and an import cycle
there is a crash that mocked tests never see."""

import importlib
import sys
import unittest

ENTRIES = (
    "unbake.cycle.engine",
    "unbake.typemap.mapping",
    "unbake.extract",
    "unbake.typemap.facts",
    "unbake.typemap.database",
    "unbake.land",
    "unbake.steps",
    "unbake.work.plan",
)


class ImportOrderTests(unittest.TestCase):
    def test_each_entry_imports_first_without_a_cycle(self) -> None:
        saved = dict(sys.modules)
        try:
            for entry in ENTRIES:
                with self.subTest(entry):
                    for name in [name for name in sys.modules if name == "unbake" or name.startswith("unbake.")]:
                        del sys.modules[name]
                    importlib.import_module(entry)
        finally:
            sys.modules.clear()
            sys.modules.update(saved)


if __name__ == "__main__":
    unittest.main()
