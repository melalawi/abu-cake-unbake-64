"""Host scratch rejects project paths and owns cleanup on success and failure."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import scratch
from unbake.config import Held


class ScratchTests(ProjectCase):
    def test_tmp_environment_and_python_tempdir_do_not_choose_the_root(self):
        with (
            patch.dict(os.environ, {"TMPDIR": str(self.project.root), "TMP": str(self.project.root)}),
            patch("tempfile.tempdir", str(self.project.root)),
        ):
            for fails in (False, True):
                with self.subTest(fails=fails):
                    try:
                        with scratch.temporary(self.host, self.project, "proof", prefix="proof-") as name:
                            path = Path(name)
                            self.assertEqual(path.parent, self.host.cache_machine_root.resolve())
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                            (path / "payload.c").write_text("int f(void) { return 1; }\n")
                            if fails:
                                raise RuntimeError("proof refused")
                    except RuntimeError:
                        pass
                    self.assertFalse(path.exists())

    def test_inside_or_symlinked_machine_cache_is_a_named_config_refusal(self):
        link = self.root / "cache-link"
        link.symlink_to(self.project.build, target_is_directory=True)
        for directory in (self.project.root, self.project.build / "cache", link):
            with self.subTest(directory=directory):
                host = SimpleNamespace(cache_machine_root=directory)
                with (
                    self.assertRaisesRegex(Held, "cache.machine_root:.*outside project.root"),
                    scratch.temporary(host, self.project, "proof", prefix="proof-"),
                ):
                    self.fail("invalid root accepted")
