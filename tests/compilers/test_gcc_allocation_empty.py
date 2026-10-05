"""A function that allocates no pseudo register is an empty allocation, not a malformed dump."""

import unittest

from unbake.compilers.families.gcc.allocation import allocation
from unbake.config import Held

NONE = ";; Register dispositions:\n\n;; "
USED = "Register 60 used 3 times across 5 insns\n"


class EmptyAllocation(unittest.TestCase):
    def test_no_usage_rows_and_no_dispositions_means_no_pseudos(self) -> None:
        self.assertEqual(allocation({"lreg": "(nothing)", "greg": NONE}).pseudos, ())

    def test_a_disposition_without_usage_is_still_refused(self) -> None:
        with self.assertRaises(Held):
            allocation({"lreg": "(nothing)", "greg": ";; Register dispositions:\n60 in 2\n\n;; "})

    def test_a_missing_dispositions_section_is_still_refused(self) -> None:
        with self.assertRaisesRegex(Held, "dumps.greg.dispositions"):
            allocation({"lreg": "(nothing)", "greg": "text"})

    def test_usage_without_a_disposition_is_still_refused(self) -> None:
        with self.assertRaises(Held):
            allocation({"lreg": USED, "greg": NONE})


if __name__ == "__main__":
    unittest.main()
