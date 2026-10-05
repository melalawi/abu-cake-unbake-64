"""draft writes build/work/FUNC/FUNC.c; a complete draft that does not compile yet is still written."""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.config import Held
from unbake.work import draft


class DraftFileTests(ProjectCase):
    def run_draft(self, m2c_draft: object) -> Path:
        with ExitStack() as stack:
            stack.enter_context(patch.object(draft.exclusions, "load", return_value=set()))
            stack.enter_context(patch.object(draft.split, "holding_versions", return_value=("us",)))
            stack.enter_context(patch.object(draft.type_context, "snapshot", return_value=("d", "")))
            stack.enter_context(patch.object(draft.extract, "directory", return_value=self.root))
            stack.enter_context(patch.object(draft.m2c, "draft", side_effect=m2c_draft))
            return draft.draft(self.project, self.host, "alpha", replace=False).file

    def test_proved_draft_is_written_with_its_include_directory(self) -> None:
        file = self.run_draft(lambda *args, **kwargs: "int alpha(void) { return 0; }\n")
        self.assertEqual(file.read_text(), "int alpha(void) { return 0; }\n")
        self.assertTrue((file.parent / "include").is_dir())

    def test_unproven_draft_is_written_and_named(self) -> None:
        def unproven(*args: object, **kwargs: object) -> str:
            scratch = Path(str(args[4]))
            (scratch / "compile-proof").mkdir(parents=True)
            (scratch / "compile-proof" / "alpha.c").write_text("int alpha(void) { return f(); }\n")
            raise Held("m2c", "alpha: m2c/type compile proof failed: too few arguments")

        with self.assertRaises(Held) as raised:
            self.run_draft(unproven)
        file = self.project.work / "alpha" / "alpha.c"
        self.assertEqual(raised.exception.key, "draft.unproven")
        self.assertIn("too few arguments", raised.exception.reason)
        self.assertEqual(file.read_text(), "int alpha(void) { return f(); }\n")
        self.assertFalse((file.parent / ".m2c").exists())

    def test_refusal_without_a_draft_writes_nothing(self) -> None:
        def refused(*args: object, **kwargs: object) -> str:
            raise Held("m2c", "alpha: unresolved M2C_ERROR at line 3")

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
        with patch.object(compare, "published", return_value=True):
            text = draft.published_seed(self.project, "alpha")
        self.assertIsNotNone(text)
        self.assertNotIn("#define", text)
        self.assertNotIn("M2C_FIELD", text)
        self.assertIn("->value", text)
        self.assertIn("[-1].value", text)
        header = self.project.work / "alpha/include/common/draft_fields_alpha.h"
        self.assertIn("padding[8]", header.read_text())
        self.assertFalse((self.project.include[0] / "common/draft_fields_alpha.h").exists())
