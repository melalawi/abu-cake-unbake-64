"""Next: lines name --config only when the environment would not resolve to it."""

import io
import os
from unittest.mock import patch

from tests.kit import TempCase
from unbake import config
from unbake.cli.args import Context


class NextLineTests(TempCase):
    def test_config_flag_follows_the_environment_default(self) -> None:
        default = self.root / "default.toml"
        with patch.dict(os.environ, {"UNBAKE_CONFIG": str(default)}):
            self.assertEqual(config.host_path(None), default)
            cases = {
                "equal to the default": (default, "unbake draft alpha"),
                "different": (self.root / "other.toml", f"unbake --config {self.root / 'other.toml'} draft alpha"),
                "none": (None, "unbake draft alpha"),
            }
            for name, (path, expected) in cases.items():
                with self.subTest(name):
                    context = Context("draft", None, None, path, io.StringIO())  # type: ignore[arg-type]
                    self.assertEqual(context.cmd("draft", "alpha"), expected)
