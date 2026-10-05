"""The hygiene rule: human text goes through the tui package, never the process's error stream."""

import unittest
from pathlib import Path

from unbake.project import write_hygiene


class StreamRule(unittest.TestCase):
    def test_a_fixture_file_fails_and_the_tui_package_passes(self) -> None:
        text = "import sys\n\nsys." + "stderr.write('hello')\n"
        failed = write_hygiene.stream_violations(Path("steps.py"), text)
        self.assertEqual(len(failed), 1)
        self.assertIn("steps.py:3", failed[0])
        self.assertEqual(write_hygiene.stream_violations(Path("tui/output.py"), text), [])
        self.assertEqual(write_hygiene.stream_violations(Path("steps.py"), "print_help()\nresult.stderr.strip()\n"), [])


if __name__ == "__main__":
    unittest.main()
