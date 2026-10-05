"""The types step names each long single-core phase on stderr."""

import io
import unittest
from contextlib import redirect_stderr

from unbake.typemap import solver


class PhaseLines(unittest.TestCase):
    def test_one_line_per_phase_on_stderr_only(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            solver.phase("inferring types (single-core)")
            solver.phase("rendering and publishing the solution (single-core)")
        self.assertEqual(
            err.getvalue().splitlines(),
            ["types: inferring types (single-core)", "types: rendering and publishing the solution (single-core)"],
        )


if __name__ == "__main__":
    unittest.main()
