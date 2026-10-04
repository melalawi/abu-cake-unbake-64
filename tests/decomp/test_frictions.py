"""Public recovery paths for draft, guidance, guards, and stored trial evidence."""

import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import assemble, assembly, fixture
from unbake.decomp import checks, declarations, drafts, guide, m2c, needs, trial, work
from unbake.layout import shared
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held


class FrictionTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.objdiff_fixture import install

        install(self)
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project, self.policy, self.source = fixture(self.root, case=self)
        self.policy.state_root = self.root / "state"
        self.policy.m2c = self.root / "m2c"
        self.policy.m2c.write_text(
            '#!/bin/sh\nprintf "void alpha(void *a) { M2C_FIELD(a, s32 *, 0x40) = 0; '
            'M2C_FIELD(a, s32 *, 0x44) = 0; }\\n"\n'
        )
        self.policy.m2c.chmod(0o755)

    def test_1_unresolved_fields_compile_with_measured_shared_views(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            source = m2c.draft(self.project, self.policy, "alpha", "us", self.project.work)
        self.assertIn(str(source), output.getvalue())
        self.assertNotIn("M2C_FIELD", source.read_text())
        self.assertNotIn("Draft_", source.read_text())
        self.assertNotIn("struct Layout_", source.read_text())
        self.assertIn("->value", source.read_text())
        self.assertFalse((self.project.include[0] / "alpha_fields.h").exists())
        staged = work.compilation_project(self.project, source)
        self.assertFalse((staged.include[0] / "structs.h").exists())
        self.assertFalse((self.project.include[0] / "structs.h").exists())
        with redirect_stdout(io.StringIO()):
            repeated = m2c.draft(self.project, self.policy, "alpha", "us", self.project.work)
        self.assertEqual(source.read_text(), repeated.read_text())
        records = layouts(source, project=staged, policy=self.policy, version="us")
        self.assertEqual(fold(records, staged), [])
        self.assertTrue(records)
        self.assertTrue(all(record.name.startswith("Measured_") for record in records))
        self.assertTrue(work.overlay_data(self.project, source)["edits"])

    def test_function_header_moves_to_shared_home_without_changing_layout(self) -> None:
        self.project.src.mkdir(parents=True, exist_ok=True)
        self.source = self.project.src / "alpha.c"
        header = self.project.include[0] / "func_800A42A4.h"
        header.write_text(
            '#ifndef FUNC_H\n#define FUNC_H\n#include "types.h"\n'
            "struct Existing { char padding[0xF4]; s32 value; };\n#endif\n"
        )
        self.source.write_text('#include "func_800A42A4.h"\ns32 alpha(struct Existing *a) { return a->value; }\n')
        before = layouts(self.source, project=self.project, policy=self.policy, version="us")
        shared.consolidate(self.project)
        after = layouts(self.source, project=self.project, policy=self.policy, version="us")
        self.assertEqual(
            [(r.name, r.size, r.fields[1].offset) for r in before],
            [(r.name, r.size, r.fields[1].offset) for r in after],
        )
        self.assertFalse(header.exists())
        self.assertIn('#include "structs.h"', self.source.read_text())
        self.assertEqual(fold(after, self.project), [])
        original = (self.project.include[0] / "structs.h").read_bytes()
        shared.consolidate(self.project)
        self.assertEqual((self.project.include[0] / "structs.h").read_bytes(), original)

    def test_function_header_with_other_declarations_is_preserved(self) -> None:
        header = self.project.include[0] / "func_800A42A4.h"
        content = "#ifndef FUNC_H\n#define FUNC_H\nstruct Existing { int value; };\nint other(void);\n#endif\n"
        header.write_text(content)
        shared.consolidate(self.project)
        self.assertEqual(header.read_text(), content)

    def test_2_missing_assembly_names_generation_command(self) -> None:
        (self.project.asm / "us/nonmatchings/alpha.s").unlink()
        with self.assertRaises(Held) as error:
            m2c.draft(self.project, self.policy, "alpha", "us", self.project.work)
        self.assertIn(
            shlex.join(["make", "-C", str(self.project.root), "VERSION=us", "extract"]), error.exception.reason
        )

    def test_3_guide_names_shared_need_and_working_resolution(self) -> None:
        self.source.write_text("struct Missing { int value; };\nint alpha(void) { return 1; }\n")
        pending = needs.LayoutNeed("us", "Missing", [], str(self.source), {})
        with (
            patch.object(guide, "load_policy", return_value=self.policy),
            patch.object(drafts.Store, "rows", return_value=[{"needs": [needs.encode(pending)]}]),
            redirect_stdout(io.StringIO()),
        ):
            output = guide.render([pending])
        self.assertIn("Missing (LayoutNeed)", output)
        header = declarations.promote(self.project, self.policy, self.source, "us")
        self.assertIn("struct Missing", header.read_text())
        self.assertEqual(header, self.project.include[0] / "structs.h")
        self.assertFalse((self.project.include[0] / "alpha_declarations.h").exists())
        self.assertEqual(
            fold(layouts(self.source, project=self.project, policy=self.policy, version="us"), self.project), []
        )

    def test_4_next_command_retains_policy(self) -> None:
        def compile_source(project: object, policy: object, source: Path, version: str, out: Path) -> Path:
            built = assemble(out.parent, "compiled", assembly("alpha", [0x24020001, 0x03E00008, 0]))
            out.write_bytes(built.read_bytes())
            return out

        policy = self.root / "policy with spaces.toml"
        with (
            patch.dict(os.environ, {"UNBAKE_POLICY": str(policy)}),
            patch("unbake.project.build.compile_object", side_effect=compile_source),
            redirect_stdout(io.StringIO()),
        ):
            result = trial.try_draft(self.project, self.policy, self.source, self.project.work)
        command = shlex.split(result.next_command)
        self.assertEqual(command[command.index("--policy") + 1], str(policy))
        self.assertIn("submit", command)

    def test_5_raw_offset_refusal_names_accepted_field_access(self) -> None:
        finding = checks.run("return *(s32*)((u8*)a+0xF4);")[0]
        self.assertIn("pointer->field", finding.text)
        self.assertIn("shared paths.include header", finding.text)
        self.assertEqual(checks.run("return a->value;"), [])

    def test_invalid_trial_record_refuses_and_preserves_evidence(self) -> None:
        store = drafts.Store(self.policy, self.project)
        store.root.mkdir(parents=True)
        path = store.root / "trials.jsonl"
        row = dict.fromkeys(
            (
                "function",
                "source_sha256",
                "sha256",
                "compares",
                "score",
                "preconditions",
                "next_command",
                "identical_everywhere",
                "at",
            ),
            None,
        )
        content = json.dumps(row) + "\n"
        path.write_text(content)
        with self.assertRaises(Held) as error:
            store.history()
        self.assertIn("work is missing", error.exception.reason)
        self.assertEqual(path.read_text(), content)
