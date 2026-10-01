"""Interior aggregate references use the declared global base."""

from unittest.mock import patch

from tests.cli.support import MainCase
from unbake.decomp.guide_layout import resolve
from unbake.decomp.symbols import Reference


class GuideLayoutTests(MainCase):
    def test_interior_reference_uses_layout_extent(self) -> None:
        for declaration in (
            "extern State global;",
            "extern const State global;",
            "extern struct State volatile global;",
        ):
            with self.subTest(declaration=declaration):
                text = "typedef int s32; typedef struct State { char pad[0x1834]; s32 frozen; } State; " + declaration
                self.source.write_text(text)
                refs = [Reference(0x80146894, "s32", 4, 0), Reference(0x80146898, "s32", 4, 4)]
                with patch("unbake.decomp.guide_layout.preprocess", return_value=text):
                    settled, output = resolve(
                        self.project, self.policy, self.source, "us", {"global": 0x80145060}, refs
                    )
                self.assertEqual(settled, {0x80146894})
                self.assertIn("global+0x1834 (global.frozen)", output[0])
