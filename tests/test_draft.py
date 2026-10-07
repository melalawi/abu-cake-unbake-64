"""draft writes build/work/FUNC/FUNC.c; a complete draft that does not compile yet is still written."""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.process import named
from unbake.typemap import database
from unbake.work import draft


class DraftFileTests(ProjectCase):
    def run_draft(self, m2c_draft: object) -> Path:
        with ExitStack() as stack:
            stack.enter_context(patch.object(draft.exclusions, "load", return_value=set()))
            stack.enter_context(patch.object(draft.split, "holding_versions", return_value=("us",)))
            stack.enter_context(patch.object(database, "digest", return_value="d"))
            stack.enter_context(patch.object(database, "context", return_value=""))
            stack.enter_context(patch.object(draft.extract, "directory", return_value=self.root))
            stack.enter_context(patch.object(draft.m2c, "draft", side_effect=m2c_draft))
            return draft.draft(self.project, self.host, "alpha", replace=False, expected_output=None).file

    def test_proved_draft_is_written_with_its_include_directory(self) -> None:
        file = self.run_draft(lambda *args, **kwargs: "int alpha(void) { return 0; }\n")
        self.assertEqual(file.read_text(), "int alpha(void) { return 0; }\n")
        self.assertTrue((file.parent / "include").is_dir())

    def test_unproven_draft_is_written_and_named(self) -> None:
        def unproven(*args: object, **kwargs: object) -> str:
            scratch = Path(str(args[4]))
            (scratch / "compile-proof").mkdir(parents=True)
            (scratch / "compile-proof" / "alpha.c").write_text("int alpha(void) { return f(); }\n")
            raise Held(
                named("alpha", "alpha: m2c/type compile proof failed: too few arguments", owner="fixture", stage="m2c")
            )

        with self.assertRaises(Held) as raised:
            self.run_draft(unproven)
        file = self.project.work / "alpha" / "alpha.c"
        self.assertEqual(raised.exception.key, "alpha")
        self.assertIn("too few arguments", raised.exception.reason)
        self.assertEqual(file.read_text(), "int alpha(void) { return f(); }\n")
        self.assertFalse((file.parent / ".m2c").exists())

    def test_refusal_without_a_draft_writes_nothing(self) -> None:
        def refused(*args: object, **kwargs: object) -> str:
            raise Held(named("alpha", "alpha: unresolved M2C_ERROR at line 3", owner="fixture", stage="m2c"))

        with self.assertRaises(Held) as raised:
            self.run_draft(refused)
        self.assertEqual(raised.exception.key, "alpha")
        self.assertFalse((self.project.work / "alpha" / "alpha.c").exists())

    def test_published_offset_helpers_become_shared_fields_in_the_draft(self) -> None:
        from unbake.work import compare

        (self.project.src / "alpha.c").write_text(
            '#include "types.h"\n'
            "#define FIELD(p, t, o) (*(t *)((s8 *)(p) + (o)))\n"
            "#define AT(t, p, o) (*(t *)((char *)(p) + (o)))\n"
            "#define M2C_FIELD(p, t, o) (*(t)((u8 *)(p) + (o)))\n"
            "s32 alpha(void *p) { FIELD(p, s32, 4) = AT(s32, p, 8); "
            "return M2C_FIELD(p, s32 *, -4); }\n"
        )
        for version in self.versions:
            split = self.project.version(version).split
            split.write_text(split.read_text().replace("asm, alpha]", "c, alpha]"))
        with patch.object(compare, "published", return_value=True):
            private = self.project.work / "alpha/include"
            private.mkdir(parents=True)
            (private / "stale.h").write_text("stale")
            made = draft.draft(self.project, self.host, "alpha", replace=True, expected_output=None)
            text = made.file.read_text()
        self.assertFalse((private / "stale.h").exists())
        self.assertIsNotNone(text)
        self.assertNotIn("#define", text)
        self.assertNotIn("M2C_FIELD", text)
        self.assertLess(text.index('#include "types.h"'), text.index('#include "common/draft_fields_alpha.h"'))
        self.assertIn("->value", text)
        self.assertIn("[-1].value", text)
        header = self.project.work / "alpha/include/common/draft_fields_alpha.h"
        self.assertIn("padding[8]", header.read_text())
        self.assertFalse((self.project.include[0] / "common/draft_fields_alpha.h").exists())
