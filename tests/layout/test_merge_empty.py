"""Publishing original assembly does not require a speculative C merge layout."""

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.layout import map as ownership
from unbake.layout import merge_units


class EmptyMergeTests(ProjectCase):
    def test_no_c_run_does_not_require_groups_before_the_first_type_solve(self):
        layout = self.project.root / "layout.toml"
        layout.write_bytes(ownership.encoded(ownership.Map(2, ())))
        (self.project.src / "alpha.s").write_text(".text\n.globl alpha\nalpha:\n.word 0x40024800\n")
        before = layout.read_bytes()
        self.assertEqual(merge_units.run(self.project, self.host), [])
        (self.project.src / "beta.c").write_text("int beta(void) { return 2; }\n")
        self.assertEqual(merge_units.run(self.project, self.host), [])
        self.assertEqual(layout.read_bytes(), before)
        (self.project.src / "gamma.c").write_text("int gamma(void) { return 3; }\n")
        with self.assertRaises(Held):
            merge_units.run(self.project, self.host)
