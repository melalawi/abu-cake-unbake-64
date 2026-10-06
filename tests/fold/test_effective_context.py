"""Source-associated declaration and token views honor the source unit environment."""

from dataclasses import replace
from unittest.mock import patch

from tests.preprocessor import output
from tests.project_fixture import ProjectCase
from unbake import process
from unbake.fold import rewrite_view
from unbake.typemap import declarations


class EffectiveContextTests(ProjectCase):
    versions = ("us",)

    def test_source_header_context_uses_its_unit_defines(self):
        source = self.project.src / "alpha.c"
        source.write_text("UnitType alpha(UnitType value) { return value; }\n")
        (self.project.include[0] / "types.h").write_text("#ifdef UNIT_TYPE\ntypedef int UnitType;\n#endif\n")
        project = replace(self.project, unit_flags={"alpha": ("-DUNIT_TYPE=1",)})
        with patch.object(process.subprocess, "run", side_effect=output):
            context = declarations.headers(project, self.host, "us", extra=source)
        self.assertIn("typedef int UnitType;", context)

    def test_spelling_view_expands_the_actual_unit_branch(self):
        source = self.project.src / "alpha.c"
        text = "#ifdef UNIT_TYPE\ntypedef int Local;\n#else\ntypedef int Wrong;\n#endif\n"
        source.write_text(text)
        project = replace(self.project, unit_flags={"alpha": ("-DUNIT_TYPE=1",)})
        with patch.object(process.subprocess, "run", side_effect=output):
            view = rewrite_view.prepare(project, self.host, text, "us", source, contents={})
        self.assertIn("Local\n", view.text)
        self.assertNotIn("Wrong\n", view.text)
