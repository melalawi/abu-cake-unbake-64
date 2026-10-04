"""Per-step prerequisites survive publication and invalidate only their consumers."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture
from unbake.match import staging
from unbake.project import makefile, setup


class CompileScopeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_settings_project_each_compiler_version_and_unit(self):
        project, _ = fixture(self.root, case=self)
        data = makefile.description(project)
        default = data["default_compiler"]
        data["compilers"]["other"] = copy.deepcopy(data["compilers"][default])
        data["macros"]["other-version"] = ["OTHER"]
        (project.src / "other.c").write_text("other")
        data["units"]["other"] = "other"
        with patch.object(makefile, "description", return_value=data):
            baseline = makefile.compile_settings(project)
        for field, change, expected in (
            ("link layout", lambda item: item["resident_mappings"].update(anything=[]), set()),
            (
                "compiler flags",
                lambda item: item["compilers"][default]["cflags"].append("-O0"),
                {f"tools/compile/{version}/{default}.json" for version in ("us", "other-version")},
            ),
            (
                "other compiler",
                lambda item: item["compilers"]["other"]["cflags"].append("-O0"),
                {f"tools/compile/{version}/other.json" for version in ("us", "other-version")},
            ),
            (
                "version macros",
                lambda item: item["macros"]["other-version"].append("CHANGED"),
                {f"tools/compile/other-version/{ident}.json" for ident in (default, "other")},
            ),
            ("unit flags", lambda item: item["unit_cflags"].update(other=["-O0"]), {"tools/compile/units/other.json"}),
            (
                "compiler selection",
                lambda item: item["units"].update(middle="other"),
                {"tools/compile/units/middle.json"},
            ),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(data)
                change(changed)
                with patch.object(makefile, "description", return_value=changed):
                    after = makefile.compile_settings(project)
                self.assertEqual({name for name in baseline if baseline[name] != after[name]}, expected)
        changed = copy.deepcopy(data)
        changed["unit_cflags"]["other"] = ["-O0"]
        with patch.object(makefile, "description", return_value=changed):
            before = makefile.compile_settings(project)
        self.assertNotEqual(before["tools/compile/units/other.json"], baseline["tools/compile/units/other.json"])

    def test_publish_helper_change_keeps_compile_prerequisites_and_receipts(self):
        project, _ = fixture(self.root, case=self)
        setup.publish_files(project, makefile.render(project))
        paths = list((project.tools / "compile").rglob("*"))
        before = {path: path.stat().st_mtime_ns for path in paths if path.is_file()}
        receipt = project.build / "us/obj/src/middle.built"
        receipt.parent.mkdir(parents=True)
        receipt.write_bytes(b"warm")
        os.utime(receipt, ns=(100, 100))
        files = makefile.render(project)
        files["tools/pool_slices.py"] += "\n# link-only publication\n"
        setup.publish_files(project, files)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})
        self.assertEqual(receipt.stat().st_mtime_ns, 100)
        graph = files["Makefile"]
        for line in graph.splitlines():
            if line.startswith("$(BUILD)/obj/src/%.built:"):
                self.assertNotIn("$(RECIPE)", line)
                self.assertNotIn("$(DRIVERS)", line)
                self.assertNotIn("unit-ranges", line)
        self.assertIn(
            "$(TOOLS)/pool_slices.py", next(line for line in graph.splitlines() if line.startswith("$(ELF):"))
        )
        self.assertNotIn("compile.py", makefile.compile_rules(project))
        self.assertNotIn("compiler.sha256", makefile.compile_rules(project))
        self.assertIn("$(TOOLS)/extract.py", next(line for line in graph.splitlines() if line.startswith("$(ELF):")))

    def test_helper_edits_publish_graph_and_scoped_inputs_with_verified_pins(self):
        project, _ = fixture(self.root, case=self)
        setup.publish_files(project, makefile.render(project))
        original = makefile.description(project)
        changed = copy.deepcopy(original)
        changed["unit_cflags"]["middle"] = ["-O0"]
        with patch.object(makefile, "description", return_value=changed):
            edits = staging.helper_edits(project)
        self.assertEqual(
            {edit.path.relative_to(project.root).as_posix() for edit in edits},
            {"tools/compile/units/middle.json", "tools/compiler.sha256"},
        )
        staging.write_staged(project, edits)
        self.assertEqual(json.loads((project.tools / "compile/units/middle.json").read_text())["flags"], ["-O0"])
        # Updating generated graph text is permitted without a global object pin.
        graph = project.root / "Makefile"
        graph.write_text("old graph\n")
        edits = staging.helper_edits(project)
        self.assertIn(graph, {edit.path for edit in edits})
        staging.write_staged(project, edits)
        self.assertEqual(graph.read_text(), makefile.render(project)["Makefile"])

    def test_link_and_extract_settings_ignore_compiler_and_unit_flags(self):
        project, _ = fixture(self.root, case=self)
        data = makefile.description(project)
        baseline = makefile.helpers(project)
        data["compilers"][data["default_compiler"]]["cflags"].append("-O0")
        data["unit_cflags"]["middle"] = ["-O1"]
        with patch.object(makefile, "description", return_value=data):
            after = makefile.helpers(project)
        for name in ("tools/link.json", "tools/extract.json"):
            self.assertEqual(baseline[name], after[name])
        data["resident_mappings"]["us"] = [{"object": "test"}]
        with patch.object(makefile, "description", return_value=data):
            after = makefile.helpers(project)
        self.assertNotEqual(baseline["tools/link.json"], after["tools/link.json"])
        self.assertEqual(baseline["tools/extract.json"], after["tools/extract.json"])

    def test_chunk_receipts_follow_only_their_own_inputs(self):
        cases = (
            ("pool_slices.py", set()),
            ("cache.py", set()),
            ("compile.py", set()),
            ("compiler.sha256", set()),
            ("compile/drivers/codegen.cc.native.sha256", {"src/middle", "src/other"}),
            ("compile/drivers/codegen.as.native.sha256", {"asm/first"}),
            ("compile/drivers/codegen.py.sha256", set()),
            ("compile/drivers/elf.py.sha256", {"src/middle", "src/other"}),
            ("compile/drivers/sn64_cc.py.sha256", set()),
            ("compile/binaries/default.sha256", {"src/middle"}),
            ("compile/binaries/other.sha256", {"src/other"}),
            ("compile/us/default.json", {"src/middle"}),
            ("compile/us/other.json", {"src/other"}),
            ("compile/us/assembly.json", {"asm/first"}),
            ("compile/units/middle.json", {"src/middle"}),
            ("symbols", set()),
        )
        for index, (changed, expected) in enumerate(cases):
            with self.subTest(changed=changed):
                root = self.root / str(index)
                tools = root / "tools"
                generation = root / "build/us.0"
                for name, _ in cases:
                    path = tools / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("")
                    os.utime(path, ns=(100, 100))
                recipe = {
                    "default_compiler": "default",
                    "units": {"other": "other"},
                    "assembly_compiler": None,
                    "compilers": {name: dict(kind="ido", cc=f"tools/{name}/cc") for name in ("default", "other")},
                }
                (tools / "build.json").write_text(json.dumps(recipe))
                receipts = {}
                for name in ("src/middle", "src/other", "asm/first"):
                    path = generation / "obj" / (name + ".built")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"receipt")
                    os.utime(path, ns=(150, 150))
                    receipts[name] = path
                # Symlink receipts belong to another generation and are never removed.
                link = generation / "obj/src/shared.built"
                link.symlink_to(receipts["src/middle"])
                os.utime(tools / changed, ns=(200, 200))
                with patch.object(staging, "independent_objects") as detach:
                    staging.chunk_stale_sources(generation, tools, tools / "symbols")
                    detach.assert_called_once_with(generation)
                self.assertEqual({name for name, path in receipts.items() if not path.exists()}, expected)
                self.assertTrue(link.is_symlink())

    def test_sn64_receipts_bind_scoped_symbols_and_only_their_own_drivers(self):
        cases = (
            ("symbols", set()),
            ("asm-symbols/first.txt", {"asm/first"}),
            (
                "compile/binaries/" + hashlib.sha256(b"policy:mips_as").hexdigest() + ".sha256",
                {"src/middle", "asm/first"},
            ),
            ("compile/drivers/sn64_cc.py.sha256", {"src/middle", "asm/first"}),
            ("compile/drivers/abumasn64.sha256", {"src/middle", "asm/first"}),
            ("compile/drivers/resolve_external_branches.py.sha256", {"asm/first"}),
            ("compile/binaries/default.sha256", {"src/middle"}),
            ("compile/drivers/elf.py.sha256", {"src/middle"}),
            ("compile.py", set()),
        )
        for index, (changed, expected) in enumerate(cases):
            with self.subTest(changed=changed):
                root = self.root / str(index)
                tools = root / "tools"
                generation = root / "build/us.0"
                for name, _ in cases:
                    path = (generation if name.startswith("asm-symbols/") else tools) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("")
                    os.utime(path, ns=(100, 100))
                recipe = {
                    "default_compiler": "default",
                    "units": {},
                    "assembly_compiler": "default",
                    "compilers": {"default": dict(kind="sn64", cc="tools/default/cc", **{"as": "policy:mips_as"})},
                }
                (tools / "build.json").write_text(json.dumps(recipe))
                receipts = {}
                for name in ("src/middle", "asm/first"):
                    path = generation / "obj" / (name + ".built")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"receipt")
                    os.utime(path, ns=(150, 150))
                    receipts[name] = path
                changed_path = (generation if changed.startswith("asm-symbols/") else tools) / changed
                os.utime(changed_path, ns=(200, 200))
                staging.chunk_stale_sources(generation, tools, tools / "symbols")
                self.assertEqual({name for name, path in receipts.items() if not path.exists()}, expected)

    def test_missing_recipe_and_empty_generation_do_nothing(self):
        generation = self.root / "us.0"
        tools = self.root / "tools"
        tools.mkdir()
        with patch.object(staging, "independent_objects") as detach:
            staging.chunk_stale_sources(generation, tools, self.root / "symbols")
            detach.assert_not_called()
