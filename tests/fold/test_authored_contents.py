"""A draft's staged header views map every authored header to its staged copy."""

import unittest
from dataclasses import dataclass
from pathlib import Path

from unbake.fold.source_views import authored_contents


@dataclass
class View:
    include: tuple[Path, ...]
    work_include: tuple[Path, ...] = ()


class AuthoredContentsTests(unittest.TestCase):
    def test_a_draft_view_with_one_work_include_and_two_layout_includes_maps_every_header(self) -> None:
        work, shared, extra = Path("/p/work/f/include"), Path("/p/include"), Path("/p/include2")
        project = View((work, shared, extra), (work,))
        staged = (Path("/s/0"), Path("/s/1"), Path("/s/2"))
        # The staged view's include is longer than the project's: its roots plus the layout includes.
        local = View((*staged, shared, extra), staged)
        headers = type("H", (), {"texts": {work / "a.h": "A", shared / "b.h": "B", extra / "c.h": "C"}})()
        self.assertEqual(
            authored_contents(project, headers, local),
            {Path("/s/0/a.h"): "A", Path("/s/1/b.h"): "B", Path("/s/2/c.h"): "C"},
        )
