"""Boundary proof does not discard the existing compiler fragment capability checks."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase, make
from unbake import pool
from unbake.work import plan


class PreservedFragmentTests(ProjectCase):
    versions = ("us",)

    def selected(self, words, *, optimize=True):
        project, host = make(self.root / ("case" + str(words[0]) + str(optimize)), words)
        compiler = project.compiler_for("alpha")
        if not optimize:
            project = replace(project, compilers={compiler.id: replace(compiler, cflags=(*compiler.cflags, "-O0"))})
        with (
            patch.object(pool, "run", side_effect=lambda host, fn, items: [fn(item) for item in items]),
            patch.object(pool.Pool, "from_host", return_value=SimpleNamespace(size=2)),
        ):
            return {row.function for row in plan.candidates(project, host)}

    def test_optimized_dead_write_and_unset_fp_fragments_are_not_public_work(self):
        for words in (
            [0x3C020000, 0x24420000, 0x03E00008, 0x2484FFFF],
            [0x00040000, 0x03E00008, 0x00001021],
            [0x46010002, 0x46001081, 0x03E00008, 0xE4820008],
        ):
            with self.subTest(words=words):
                self.assertEqual(self.selected(words), {"beta", "gamma"})

    def test_nonoptimizing_and_live_values_remain_work(self):
        self.assertIn("alpha", self.selected([0x3C020000, 0x24420000, 0x03E00008, 0x2484FFFF], optimize=False))
        self.assertIn("alpha", self.selected([0x2484FFFF, 0x03E00008, 0x00801021]))
        self.assertIn("alpha", self.selected([0x10800003, 0, 0x03E00008, 0x00801025, 0x03E00008, 0x00001025]))
