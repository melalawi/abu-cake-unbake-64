"""SDK analysis resolves unit-relative forced inputs against the project root."""

from dataclasses import replace
from unittest.mock import patch

from tests.decomp.test_gbi_recover import SDK
from tests.preprocessor import output
from tests.project_fixture import ProjectCase
from unbake import process
from unbake.decomp import gbi_recover


class CatalogueDirectoryTests(ProjectCase):
    versions = ("us",)

    def test_relative_forced_macro_input_is_found_and_its_sdk_pattern_survives(self):
        (self.project.root / "flags.h").write_text("#define FLAG 1\n")
        (self.project.include[0] / "gbi.h").write_text(SDK)
        project = replace(self.project, unit_flags={"alpha": ("-imacros", "flags.h")})

        def cpp(command, work, phase, *, temporary_root=None):
            if "-imacros" not in command:
                return "#define __STDC__ 1\n"
            forced = command[command.index("-imacros") + 1]
            (work / forced).read_text()
            return SDK

        with (
            patch.object(process, "run_tool", side_effect=cpp),
            patch.object(process.subprocess, "run", side_effect=output),
        ):
            patterns = gbi_recover.catalogue(project, self.host, project.src / "alpha.c", "us", '#include "gbi.h"\n')
        self.assertIn("gDPSync", [pattern.macro.name for pattern in patterns])
