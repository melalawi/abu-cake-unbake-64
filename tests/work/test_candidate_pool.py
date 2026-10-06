"""plan.candidates sends its word rules through the pool one group at a time; each verdict equals the serial rules.
The declared-header digest reads cpp line markers relative to the tree root."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.work.test_shape import EMITTED, SHAPES, owned, words
from unbake.typemap import declarations
from unbake.work import plan

TARGET = SHAPES["gcc-2.8.1-sn64"]
FRAMED = words("27bdffe8 afbf0014 0c000000 00000000 8fbf0014 27bd0018 03e00008 00000000")
FILLER = words("8ae3e172 aea18b2f") + FRAMED
OPEN = words("27bdffe8 afbf0014 0c000000 00000000")


class DrafterVerdictTests(unittest.TestCase):
    def test_each_verdict_equals_the_serial_rules(self) -> None:
        placements = [((FRAMED, 0x80001000),), ((FILLER, 0x8008EC78),), ((FRAMED, 0x80001000), (FILLER, 0x8008EC78))]
        groups = [
            (TARGET, EMITTED, tuple((body, address, owned(body, address, TARGET)) for body, address in bodies))
            for bodies in placements
        ]
        self.assertEqual([plan._drafter(group) for group in groups], [True, False, False])


class DeclaredDigestTests(unittest.TestCase):
    def test_line_markers_read_relative_to_the_tree(self) -> None:
        text = '# 1 "{root}/include/common/types.h"\ntypedef int s32; /* a/b */\n'
        seen = set()
        for root in ("/a/bt", "/x/y/z/bt-longer"):
            project = SimpleNamespace(root=Path(root))
            seen.add(declarations.rooted(project, text.format(root=root)))  # type: ignore[arg-type]
        self.assertEqual(seen, {'# 1 "include/common/types.h"\ntypedef int s32; /* a/b */\n'})
