"""Source ownership follows the compilation environment and stays local to its consumer."""

from dataclasses import replace
from unittest.mock import patch

from tests.preprocessor import output
from tests.project_fixture import ProjectCase
from unbake import process
from unbake.typemap import header_names


class EffectiveOwnershipTests(ProjectCase):
    versions = ("us",)

    def test_compiler_and_unit_defines_select_only_owned_declarations(self):
        source = self.project.src / "alpha.c"
        source.write_text(
            '#include "types.h"\n#ifdef COMPILER_TYPE\ntypedef int FromCompiler;\n#endif\n'
            "#ifdef UNIT_TYPE\nstruct FromUnit { int value; };\n#endif\n"
            "int alpha(void) { return 0; }\n"
        )
        compiler = self.project.compiler_for("alpha")
        project = replace(
            self.project,
            compilers={compiler.id: replace(compiler, cflags=(*compiler.cflags, "-DCOMPILER_TYPE=1"))},
            unit_flags={"alpha": ("-DUNIT_TYPE=1",)},
        )
        with patch.object(process.subprocess, "run", side_effect=output):
            names, tags = header_names._owned((project, self.host, source, source.read_text()))
        self.assertIn("FromCompiler", names)
        self.assertIn("FromUnit", tags)
        self.assertNotIn("s32", names)
        other = self.project.src / "beta.c"
        other.write_text(source.read_text())
        with patch.object(process.subprocess, "run", side_effect=output):
            _, other_tags = header_names._owned((project, self.host, other, other.read_text()))
        self.assertNotIn("FromUnit", other_tags)

    def test_forced_header_selects_local_type_without_becoming_local_ownership(self):
        forced = self.project.include[0] / "forced.h"
        forced.write_text("#define PRIVATE_TYPE 1\ntypedef int Imported;\n")
        source = self.project.src / "alpha.c"
        source.write_text("#ifdef PRIVATE_TYPE\ntypedef int Local;\n#endif\nint alpha(void) { return 0; }\n")
        project = replace(self.project, unit_flags={"alpha": ("-include", str(forced))})
        with patch.object(process.subprocess, "run", side_effect=output):
            names, _ = header_names._owned((project, self.host, source, source.read_text()))
        self.assertIn("Local", names)
        self.assertNotIn("Imported", names)

    def test_included_header_can_undefine_a_compiler_macro_before_local_declarations(self):
        (self.project.include[0] / "types.h").write_text("#undef PRIVATE_TYPE\ntypedef int Imported;\n")
        source = self.project.src / "alpha.c"
        source.write_text(
            '#include "types.h"\n#ifdef PRIVATE_TYPE\ntypedef int Wrong;\n'
            "#else\ntypedef int Right;\n#endif\nint alpha(void) { return 0; }\n"
        )
        project = replace(self.project, unit_flags={"alpha": ("-DPRIVATE_TYPE=1",)})
        with patch.object(process.subprocess, "run", side_effect=output):
            names, _ = header_names._owned((project, self.host, source, source.read_text()))
        self.assertEqual(names, ["Right"])
