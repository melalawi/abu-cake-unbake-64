"""A draft cannot break existing header consumers before proving compilation."""

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.layout.structs import layouts
from unbake.layout.structs_fold import fold
from unbake.project.config import Held


class HeaderSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import tempfile

        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project, _, _ = fixture(self.root, versions=("us", "eu"), case=self)
        import subprocess

        from tests.process_fakes import boundary
        from unbake.decomp import trial_compile

        self.failed_version = None
        self.failed_mode = None
        self.diagnostic = "fixture compiler refusal"
        self.commands = []

        def compiler(command, **kwargs):
            self.commands.append(command)
            failed = self.failed_version and "-DVERSION_" + self.failed_version.upper() in command
            failed = failed and (self.failed_mode is None or ("-DNON_MATCHING=1" in command) == self.failed_mode)
            if "-o" in command and not failed:
                Path(command[command.index("-o") + 1]).write_bytes(b"object fixture")
            return subprocess.CompletedProcess(command, int(bool(failed)), "", self.diagnostic if failed else "")

        mock = boundary(trial_compile, compiler)
        mock.start()
        self.addCleanup(mock.stop)
        self.project = replace(
            self.project,
            version_map={
                name: replace(version, macros=("VERSION_" + name.upper(),))
                for name, version in self.project.version_map.items()
            },
        )
        verification = patch("unbake.project.toolchain.verify")
        verification.start()
        self.addCleanup(verification.stop)
        self.header = self.project.include[0] / "structs.h"
        self.before = (
            '#ifndef STRUCTS_H\n#define STRUCTS_H\n#include "types.h"\n'
            "struct Existing { int value; };\ntypedef struct Existing Existing;\n"
            "extern Existing external;\n#endif\n"
        )
        self.header.write_text(self.before)
        self.wrapper = self.project.include[0] / "wrapper.h"
        self.wrapper.write_text('#include "structs.h"\n')
        self.unit = self.project.src / "unrelated.c"
        self.unit.write_text('#include "wrapper.h"\nint unrelated(void) { return external.value; }\n')

    def test_extending_existing_header_with_new_pointer_type_preserves_aliases(self) -> None:

        self.header.write_text(
            "#ifndef STRUCTS_H\n#define STRUCTS_H\n"
            "struct Node {int value;};\n"
            "struct Owner {unsigned char pad0[96];struct Node *nodes;};\n#endif\n"
        )
        self.unit.write_text('#include "wrapper.h"\nint unrelated(struct Owner *p) {return p->nodes->value;}\n')
        destination = self.project.include[0] / "shared/slots.h"
        records = layouts(
            "typedef struct Slot {char pad0[40];int value;char pad2[4];} Slot;\n"
            "typedef struct Owner {char pad0[60];int current;Slot *slots;} Owner;\n"
        )
        edits = fold(records, self.project, destination=destination)
        extended = next(edit.after for edit in edits if edit.path == self.header)
        self.assertIn("struct Slot *slots;", extended)
        self.assertIn("typedef struct Owner Owner;", extended)
        for edit in edits:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)
        consumer = self.project.src / "consumer.c"
        consumer.write_text(
            '#include "wrapper.h"\n#include "shared/slots.h"\n'
            "int consumer(Owner *p) {return p->slots[p->current].value;}\n"
            "typedef char slot_size[(sizeof(Slot)==48)?1:-1];\n"
        )
        self.assertIn("struct Slot *slots;", self.header.read_text())
        self.assertIn("Slot", destination.read_text())

    def test_missing_m2c_scalar_refuses_fold_without_writing_any_header(self) -> None:
        self.unit.write_text(
            '#ifdef NON_MATCHING\n#include "wrapper.h"\nint unrelated(void) { return external.value; }\n#endif\n'
        )
        context = "typedef int M2C_UNK32;\n"
        (self.project.include[0] / "m2c_prelude.h").write_text(context)
        self.failed_version = "us"
        self.failed_mode = True
        self.diagnostic = "M2C_UNK32"
        with self.assertRaisesRegex(Held, r"(?s)structs.h.*unrelated.c VERSION us NON_MATCHING=1.*M2C_UNK32"):
            fold(layouts(context + "struct Layout_alpha_arg0 { M2C_UNK32 field_0; };"), self.project)
        self.assertEqual(self.header.read_text(), self.before)
        self.assertEqual(
            {path.name for path in self.project.include[0].glob("*.h")},
            {"types.h", "structs.h", "wrapper.h", "m2c_prelude.h"},
        )

    def test_every_version_is_checked_and_failure_keeps_existing_bytes(self) -> None:
        self.unit.write_text(
            '#include "wrapper.h"\n#ifdef VERSION_EU\n'
            "typedef char preserve_size[sizeof(Existing) == sizeof(int) ? 1 : -1];\n#endif\n"
        )
        self.failed_version = "eu"
        with self.assertRaisesRegex(Held, r"structs.h.*unrelated.c VERSION eu NON_MATCHING=0"):
            fold(layouts("struct Existing { int value; int added; };"), self.project)
        self.assertEqual(self.header.read_text(), self.before)

    def test_forced_includes_with_unit_flags_are_checked(self) -> None:
        self.unit.write_text("typedef char preserve_size[sizeof(Existing) == sizeof(int) ? 1 : -1];\n")
        path = self.project.root / "config.toml"
        path.write_text(path.read_text() + '\n[build.unit_cflags]\nunrelated=["-include", "include/structs.h"]\n')
        self.failed_version = "us"
        with self.assertRaisesRegex(Held, r"structs.h.*unrelated.c VERSION us"):
            fold(layouts("struct Existing { int value; int added; };"), self.project)
        self.assertEqual(self.header.read_text(), self.before)

    def test_computed_include_refuses_by_name_before_writing(self) -> None:
        self.unit.write_text('#define HOME "wrapper.h"\n#include HOME\n')
        with self.assertRaisesRegex(Held, r"unrelated.c.*computed include HOME"):
            fold(layouts("struct Added { int value; };"), self.project)
        self.assertEqual(self.header.read_text(), self.before)

    def test_absolute_include_cannot_bypass_proposed_header(self) -> None:
        self.unit.write_text(f'#include "{self.header}"\n')
        with self.assertRaisesRegex(Held, r"structs.h.*unrelated.c.*absolute include"):
            fold(layouts("struct Added { int value; };"), self.project)
        self.assertEqual(self.header.read_text(), self.before)

    def test_extra_include_search_directory_and_no_space_directive(self) -> None:
        extra = self.project.root / "extra"
        extra.mkdir()
        (extra / "extra.h").write_text('#include"structs.h"\n')
        self.unit.write_text('#include"extra.h"\nint unrelated(void) { return external.value; }\n')
        path = self.project.root / "config.toml"
        path.write_text(path.read_text() + '\n[build.unit_cflags]\nunrelated=["-Iextra"]\n')
        edits = fold(layouts("struct Added { int value; };"), self.project)
        self.assertEqual(len(edits), 1)
        self.assertEqual(self.header.read_text(), self.before)

    def test_addition_preserves_existing_declarations_and_proves_all_consumers(self) -> None:
        extra = self.project.src / "other.c"
        extra.write_text('#include "structs.h"\nint other(void) { return external.value; }\n')
        from unbake.decomp.trial_compile import run_tool

        with patch("unbake.decomp.trial_compile.run_tool", wraps=run_tool) as calls:
            edits = fold(layouts("struct Added { int value; };"), self.project)
        self.assertEqual(len(edits), 1)
        after = edits[0].after
        for declaration in (
            "struct Existing { int value; };",
            "typedef struct Existing Existing;",
            "extern Existing external;",
        ):
            self.assertIn(declaration, after)
        self.assertLess(after.index("struct Existing {"), after.index("typedef struct Existing Existing;"))
        self.assertLess(after.index("typedef struct Existing Existing;"), after.index("extern Existing external;"))
        self.assertEqual(calls.call_count, 8)  # Two consumers, two versions, both build modes.
        for call in calls.call_args_list:
            self.assertTrue(Path(call.args[1]).resolve().is_relative_to(self.root.parent))
            self.assertNotEqual(call.args[1], self.project.root)
        self.assertEqual(self.header.read_text(), self.before)


if __name__ == "__main__":
    unittest.main()
