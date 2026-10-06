"""Untouched private header copies follow a land; staged header edits survive."""

from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.fold import apply


class PrivateHeaderRefreshTests(ProjectCase):
    def test_refreshes_untouched_copies_and_preserves_staged_edits(self) -> None:
        shared = self.project.include[-1] / "main/alpha.h"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("extern int first(void);\n")
        owner = SimpleNamespace(owners={"alpha": SimpleNamespace(header="main/alpha.h")})
        with patch.object(apply.layout_map, "load", return_value=owner):
            local = apply.view(self.project, "alpha")
            private = local.include[0] / "main/alpha.h"
            self.assertEqual(private.read_text(), shared.read_text())
            shared.write_text("extern int second(void);\n")
            apply.view(self.project, "alpha")
            self.assertEqual(private.read_text(), shared.read_text())
            private.write_text("struct Staged { int value; };\n")
            shared.write_text("extern int third(void);\n")
            apply.view(self.project, "alpha")
            self.assertEqual(private.read_text(), "struct Staged { int value; };\n")
