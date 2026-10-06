"""Native preprocessing stays inside the repository's one atomic publication owner."""

import unittest
from pathlib import Path

from unbake.compilers import drivers
from unbake.project import write_hygiene
from unbake.typemap import declarations


class PreprocessPublicationTests(unittest.TestCase):
    def test_all_preprocess_wrappers_follow_the_existing_write_contract(self):
        root = Path(drivers.__file__).parents[1]
        for module in (drivers, declarations):
            path = Path(module.__file__)
            with self.subTest(module=module.__name__):
                self.assertEqual(write_hygiene.violations(path.relative_to(root), path.read_text()), [])
