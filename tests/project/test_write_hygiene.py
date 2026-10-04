"""Keep generated writes safe even when destinations flow through parameters."""

import unittest
from pathlib import Path

from unbake.project.write_hygiene import makefile_violations, violations


class WriteHygieneTests(unittest.TestCase):
    def check(self, code):
        return violations(Path("project/new_writer.py"), code)

    def test_direct_and_indirect_writers_fail(self):
        for code in (
            'path.write_text("record")',
            'path.write_bytes(b"record")',
            "path.touch()",
            "import os\nos.open(path, os.O_WRONLY | os.O_TRUNC)",
            'import os\nos.fdopen(descriptor, "w")',
            'with (project.build / "record").open("w") as output: pass',
            'with path.open(mode="a") as output: pass',
            "with path.open(mode) as output: pass",
            'with open(path, "wb") as output: pass',
            "import shutil as files\nfiles.copy2(source, destination)",
            "from shutil import copyfile as copy\ncopy(source, destination)",
            "import shutil\nshutil.copytree(source, destination, dirs_exist_ok=True)",
            'import tarfile\nwith tarfile.open(path, "w") as output: pass',
        ):
            with self.subTest(code=code):
                self.assertTrue(self.check(code))

    def test_atomic_publication_reads_and_exclusive_creation_pass(self):
        for code in (
            'atomic_files.text(path, "record")',
            'atomic_files.write(path, b"record")',
            'with atomic_files.stream(path, "a") as output: output.write("record")',
            'with path.open("rb") as source: pass',
            'with path.open("x") as output: pass',
            "import tarfile\nwith atomic_files.staging(path) as pending:\n"
            ' with tarfile.open(pending, "w") as output: pass',
        ):
            with self.subTest(code=code):
                self.assertEqual(self.check(code), [])

    def test_lock_exception_does_not_allow_other_writes(self):
        path = Path("project_tools/compile_identity.py")
        self.assertEqual(violations(path, '(path.parent / ".identity.lock").open("a")'), [])
        self.assertTrue(violations(path, '(path.parent / "record").open("a")'))
        self.assertTrue(violations(path, '(path.parent / ".identity.lock").open("w")'))

    def test_variable_lock_exception_is_limited_to_the_lock_function(self):
        path = Path("project/build.py")
        self.assertTrue(violations(path, 'def writer(path):\n with path.open("a+b") as output: pass'))
        self.assertEqual(violations(path, 'def _lock(path):\n with path.open("a+b") as output: pass'), [])

    def test_recipe_writes_and_compiler_outputs_must_be_wrapped(self):
        for command in (
            "$(CC) -c $< -o $@",
            "$(AS) -o $@ $<",
            "$(LD) -o $@ $<",
            "$(OBJCOPY) -O binary $< $@",
            "echo result > $(BUILD)/record",
            "cp $< $@",
            "touch $@",
        ):
            with self.subTest(command=command):
                self.assertTrue(makefile_violations("\t" + command))
        for command in (
            "python3 $(TOOLS)/atomic.py --output $@ -- $(LD) -o $@ $<",
            "python3 $(TOOLS)/atomic.py --touch $@",
            "sha256sum -c $(PINS) > /dev/null",
        ):
            with self.subTest(command=command):
                self.assertEqual(makefile_violations("\t" + command), [])
