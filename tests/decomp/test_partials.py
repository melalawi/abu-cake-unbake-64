"""Guarded publication, exact landing edits, and standalone partial object builds."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from unbake.decomp import drafts
from unbake.project import makefile
from unbake.project.config import Held


def extract_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("partial_extract", makefile.TEMPLATES / "extract.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PartialsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.src = self.root / "src"
        self.src.mkdir()
        self.splits = {}
        for version in ("us", "eu"):
            path = self.root / (version + ".yaml")
            path.write_text("segments:\n  - [0x40, asm, f]\n  - [0x4C]\n")
            self.splits[version] = path
        self.project = SimpleNamespace(
            id="00000000-0000-4000-8000-000000000001",
            workspace_id="00000000-0000-4000-8000-000000000002",
            name="fixture",
            root=self.root,
            src=self.src,
            version=lambda v: SimpleNamespace(split=self.splits[v]),
        )
        self.policy = SimpleNamespace(state_root=self.root / "state")

    def add(self, text: str, score: float = 50) -> drafts.Store:
        path = self.root / "f.c"
        path.write_text(text)
        typed = dict.fromkeys(("register", "order", "immediate", "relocation", "inserted", "missing", "changed"), 0)
        trial = SimpleNamespace(
            function="f",
            work_identity={"schema": 1, "project_id": self.project.id, "workspace_id": self.project.workspace_id},
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            compares={"us": SimpleNamespace(version="us", identical=1, of=3, typed=typed, lines=[])},
            needs=[],
            preconditions=[],
            next_command="unbake submit f.c",
            identical_everywhere=False,
        )
        store = drafts.Store(self.policy, self.project)
        store.add(trial, path, {"us": score})
        return store

    def test_bulk_publish_cannot_bypass_current_trial_and_full_proof(self) -> None:
        store = self.add("void f(void) {}\n")
        path = self.src / "f.c"
        with self.assertRaisesRegex(Held, "drafts.work.source"):
            store.publish_all()
        self.assertFalse(path.exists())
        path.write_text("int f(void) { return 2; }\n")
        self.assertEqual(store.publish_all(), [])
        self.assertIn("return 2", path.read_text())

    def test_match_edits_remove_guard_and_flip_every_version(self) -> None:
        self.add("#if DEBUG\nint f(void) { return 1; }\n#endif\n")
        path = self.src / "f.c"
        path.write_text("#ifdef NON_MATCHING\n#if DEBUG\nint f(void) { return 1; }\n#endif\n#endif\n")
        before = path.read_text()
        edits = drafts.match_edits(self.project, "f", before, ("us", "eu"))
        self.assertEqual(len(edits), 3)
        self.assertEqual(edits[0].before, before)
        self.assertEqual(edits[0].after, "#if DEBUG\nint f(void) { return 1; }\n#endif\n")
        for edit in edits[1:]:
            self.assertIn("[0x40, c, f]", edit.after)
        self.assertEqual(path.read_text(), before)

    def test_missing_and_conflicting_values_are_named(self) -> None:
        for source, name in [("", "source_text"), ("int f;", "versions")]:
            with self.subTest(name=name), self.assertRaisesRegex(Held, name):
                drafts.match_edits(self.project, "f", source, ())
        for source in ["#ifdef NON_MATCHING\nint x;", "#ifdef NON_MATCHING\n#else\n#endif\n", "int x;"]:
            with self.subTest(source=source), self.assertRaisesRegex(Held, "NON_MATCHING.guard"):
                drafts.unguard(source)
        self.splits["eu"].write_text("segments:\n - [0x40, c, f]\n - [0x4C]\n")
        with self.assertRaisesRegex(Held, "requires asm"):
            drafts.match_edits(self.project, "f", "void f(void) {}", ("eu",))
        self.splits["us"].write_text("segments:\n - [0x40, asm, other]\n - [0x4C]\n")
        with self.assertRaisesRegex(Held, "one asm row"):
            drafts.match_edits(self.project, "f", "void f(void) {}", ("us",))
        (self.src / "f.c").write_text("void f(void) {}")
        with self.assertRaisesRegex(Held, "already exists"):
            drafts.match_edits(self.project, "f", "void f(void) {}", ("eu",))

    def test_optional_rows_use_guarded_drafts_only(self) -> None:
        module = extract_helper()
        text = 'segments:\n - [0x40, asm, "text/f"]\n - [0x4C, asm, other]\n - [0x58]\n'
        for content, selected in [
            ("void f(void) {}\n", False),
            ("#ifdef NON_MATCHING\nvoid f(void) {}\n#endif\n", True),
        ]:
            with self.subTest(selected=selected):
                (self.src / "text").mkdir(exist_ok=True)
                (self.src / "text/f.c").write_text(content)
                result = module.partial_rows(text, self.src)
                self.assertEqual(", c," in result, selected)
                self.assertIn("[0x4C, asm, other]", result)
                self.assertIn('asm, "text/f"', text)

    def test_standalone_partial_object_receives_macro(self) -> None:
        from tests.project.makefile_fixture import fixture, write_rendered

        (self.root / "build-fixture").mkdir()
        project, _policy = fixture(self.root / "build-fixture", case=self)
        self.addCleanup(patch.stopall)
        write_rendered(project)
        (project.src / "first.c").write_text("#ifdef NON_MATCHING\nvoid first(void) {}\n#endif\n")
        compiler = project.tools / "fixture/cc"
        code = compiler.read_text().replace(
            "a = sys.argv[1:]",
            "a = sys.argv[1:]\nwith Path('compiler-args.jsonl').open('a') as trace_args: "
            "trace_args.write(json.dumps(a) + '\\n')",
        )
        compiler.write_text(code)
        pins = project.tools / "compiler.sha256"
        entries = pins.read_text().splitlines()
        entries = [
            hashlib.sha256(compiler.read_bytes()).hexdigest() + "  tools/fixture/cc"
            if line.endswith("  tools/fixture/cc")
            else line
            for line in entries
        ]
        pins.write_text("\n".join(entries) + "\n")
        splat = project.tools / "splat"
        splat.write_text(
            splat.read_text().replace(
                "str(build/'asm/first.s.o')",
                "str(build/('src/first.c.o' if ', c, \"first\"]' in Path(a[-2]).read_text() else 'asm/first.s.o'))",
            )
        )
        from tests.helper_fixture import extraction
        from unbake.project import build

        result = extraction(project, generation=project.root / "build/us.nonmatching")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = project.root / "build/us.nonmatching/obj/src/first.o"
        with patch("unbake.project.toolchain.verify", return_value={}):
            build.compile_object(project, _policy, project.src / "first.c", "us", target, non_matching=True)
        self.assertTrue(target.is_file())
        calls = [json.loads(line) for line in (project.root / "compiler-args.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        preprocessing = [call for call in calls if "-E" in call or "-M" in call]
        codegen = [call for call in calls if "-E" not in call and "-M" not in call]
        self.assertTrue(preprocessing)
        self.assertTrue(codegen)
        self.assertTrue(all("-DNON_MATCHING=1" in call for call in preprocessing))
        self.assertTrue(all("-DNON_MATCHING=1" not in call for call in codegen))
        self.assertIn("NON_MATCHING", (project.root / "Makefile").read_text())
        self.assertIn("[0x0, asm, first]", project.version("us").split.read_text())
        self.assertFalse((project.root / "build/us/obj/src/first.o").exists())
