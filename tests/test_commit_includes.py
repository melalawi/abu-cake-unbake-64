"""A commit never leaves a tracked file including a project header the committed tree lacks.

The payloads are the BattleTanx headers a refresh left dangling: the shared type header is named by a content hash,
so a regeneration renames it and every header including it must be committed in the same step."""

import subprocess
import unittest
from types import SimpleNamespace

from tests.kit import TempCase
from unbake import land
from unbake.config import Held

OLD = "common/types_d507c48987bb.h"
NEW = "common/types_f8bfabebf96f.h"
TYPES = "struct Shape_func_800E6970_us { s32 a; };\n"
SPAN = (
    "#ifndef UNBAKE_SPAN_1000_CODE_800E5328_H\n#define UNBAKE_SPAN_1000_CODE_800E5328_H\n"
    '#include "../types.h"\n#include "@@"\nstruct Shape_func_800E6970_us;\n'
    "extern s32 func_800E69A4_us(struct Shape_func_800E6970_us *);\n#endif\n"
)
SOURCE = '#include "span_1000/code_800E5328.h"\n#include "@@"\ns32 f(void) { return 0; }\n'


class CommitIncludeTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        root = self.root
        for relative, text in (
            ("include/types.h", "typedef int s32;\n"),
            (f"include/{OLD}", TYPES),
            ("include/span_1000/code_800E5328.h", SPAN.replace("@@", OLD)),
            ("include/span_1000/code_800E6FB0.h", SPAN.replace("@@", OLD)),
            ("src/func_800E69A4_us.c", SOURCE.replace("@@", OLD)),
        ):
            self.write(relative, text)
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        self.project = SimpleNamespace(root=root, build=root / "build", include=[root / "include"])
        self.host = SimpleNamespace(publish_author_name="t", publish_author_email="t@t")

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def rename_types(self) -> None:
        (self.root / "include" / OLD).unlink()
        self.write(f"include/{NEW}", TYPES + "/* new */\n")

    def test_commit_refuses_a_header_including_a_deleted_project_header(self) -> None:
        self.rename_types()
        self.write("include/span_1000/code_800E5328.h", SPAN.replace("@@", NEW))
        names = [f"include/{OLD}", f"include/{NEW}", "include/span_1000/code_800E5328.h"]
        with self.assertRaises(Held) as raised:
            land._commit(self.project, self.host, [self.root / name for name in names], "Refresh generated files")
        self.assertIn("include/span_1000/code_800E6FB0.h", raised.exception.reason)
        self.assertIn(OLD, raised.exception.reason)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), "1")

    def test_commit_accepts_a_consistent_rename(self) -> None:
        self.rename_types()
        for header in ("code_800E5328", "code_800E6FB0"):
            self.write(f"include/span_1000/{header}.h", SPAN.replace("@@", NEW))
        self.write("src/func_800E69A4_us.c", SOURCE.replace("@@", NEW))
        names = [f"include/{OLD}", f"include/{NEW}", "include/span_1000/code_800E5328.h"]
        names += ["include/span_1000/code_800E6FB0.h", "src/func_800E69A4_us.c"]
        land._commit(self.project, self.host, [self.root / name for name in names], "Refresh generated files")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), "2")

    def test_header_left_dirty_before_the_run_and_rewritten_by_it_is_committed(self) -> None:
        self.write("include/span_1000/code_800E6FB0.h", SPAN.replace("@@", OLD) + "/* earlier step */\n")
        before = land.snapshot(self.project)
        self.rename_types()
        self.write("include/span_1000/code_800E6FB0.h", SPAN.replace("@@", NEW))
        self.write("include/span_1000/code_800E5328.h", SPAN.replace("@@", NEW))
        self.write("src/func_800E69A4_us.c", SOURCE.replace("@@", NEW))
        land.commit_generated(self.project, self.host, before, "Refresh generated files")
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        self.assertIn(NEW, self.git("show", "HEAD:include/span_1000/code_800E6FB0.h"))

    def test_path_dirty_before_and_untouched_by_the_run_stays_out_of_the_commit(self) -> None:
        self.write("include/span_1000/code_800E6FB0.h", SPAN.replace("@@", OLD) + "/* owner edit */\n")
        before = land.snapshot(self.project)
        self.write("include/span_1000/code_800E5328.h", SPAN.replace("@@", OLD) + "/* generated */\n")
        land.commit_generated(self.project, self.host, before, "Refresh generated files")
        self.assertEqual(self.git("status", "--porcelain").strip(), "M include/span_1000/code_800E6FB0.h")


if __name__ == "__main__":
    unittest.main()
