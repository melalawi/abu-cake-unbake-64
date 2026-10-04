"""Real objdiff rows, symbolic relocations, and build inventory selection."""

import io
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.decomp.support import assemble, assembly, fixture
from tests.support import test_policy
from unbake.decomp.score import diff
from unbake.decomp.trial import render, try_draft
from unbake.decomp.trial_compare import TYPES, compare_object
from unbake.decomp.trial_target import target_object
from unbake.project import build, makefile
from unbake.config import Held, Policy


class ObjectTrialTests(unittest.TestCase):
    def setUp(self):
        from tests.objdiff_fixture import install

        install(self)

    def test_trial_compiles_versions_concurrently_and_keeps_ordered_verdicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy, source = fixture(root, versions=("us", "eu"), case=self)
            policy.cores = 2
            ready = threading.Barrier(2)

            def compile_source(project, policy, source, version, out):
                ready.wait(timeout=5)
                shutil.copyfile(assemble(out.parent, "compiled", assembly("alpha", [0x24020001, 0x03E00008, 0])), out)
                return out

            with patch.object(build, "compile_object", side_effect=compile_source), redirect_stdout(io.StringIO()):
                result = try_draft(project, policy, source, root / "scratch")
            self.assertEqual(list(result.compares), ["us", "eu"])
            self.assertTrue(result.identical_everywhere)

    def test_resolved_relocations_keep_naming_separate_from_code_differences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = test_policy(root)
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "lui $v0,%hi({data})\nlw {register},%lo({data})($v0)\n"
                "jal {table}\nnop\nj {label}{offset}\nnop\n{label}:\njr $ra\nnop\n"
                ".size alpha,.-alpha\n"
            )
            cases = (
                ("field", "$v0", "map_alias", "", 0, 4, 8),
                ("base-4", "$v0", "jump_table", "", 0, 1, 8),
                ("wrong", "$v0", "map_alias", "", 2, 2, 6),
                ("unknown", "$v0", "map_alias", "", 2, 2, 6),
                ("field", "$v1", "map_alias", "", 0, 4, 7),
                ("field", "$v0", "map_alias", "+4", 1, 3, 7),
                ("field+4", "$v0", "map_alias", "", 2, 2, 6),
            )
            for version, start in (("us", 0x80008000), ("eu", 0x80208000)):
                generation = root / "build" / f"{version}.0"
                generation.mkdir(parents=True)
                symbols = root / "versions" / version / "symbol_addrs.txt"
                symbols.parent.mkdir(parents=True)
                symbols.write_text(f"base = 0x{start + 4:X};\nfield = 0x{start:X};\nwrong = 0x{start + 8:X};\n")
                (generation / "game.map").write_text(
                    f" 0x{start + 16:X} jump_table\n 0x{start + 16:X} PROVIDE (map_alias = 0x{start + 16:X})\n"
                )
                target = assemble(
                    generation,
                    "target",
                    body.format(data="base-4", register="$v0", table="jump_table", label="end_target", offset=""),
                )
                for data, register, table, offset, differences, naming, identical in cases:
                    with self.subTest(version=version, data=data, register=register, table=table, offset=offset):
                        candidate = assemble(
                            root,
                            "candidate",
                            body.format(data=data, register=register, table=table, label=".Lend_draft", offset=offset),
                        )
                        document = diff(
                            policy, version, "alpha", target, candidate, root / "diff.json", generation=generation
                        )
                        before = json.loads((root / "diff.json").read_bytes())
                        result = compare_object(version, document, "alpha")
                        self.assertEqual(result.typed["relocation"], differences)
                        self.assertEqual(result.typed["register"], int(register == "$v1"))
                        self.assertEqual(result.naming, naming)
                        self.assertEqual(result.identical, identical)
                        self.assertEqual(result.match_percent, before["left"]["symbols"][1]["match_percent"])
                        self.assertIn(f"naming: {naming} relocations equalised by address", result.lines)
                        self.assertEqual(set(result.typed), set(TYPES))

    def test_flag_probe_ranks_real_objects_per_version_without_changing_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy, source = fixture(root, versions=("us", "eu"), case=self)
            compiler = project.compiler_for(source)
            project = replace(project, compilers={compiler.id: replace(compiler, cflags=("-O2",))})
            config = project.root / "config.toml"
            build_config = (
                '[build]\nld="ld"\nobjcopy="objcopy"\nsplat="splat"\nas="as"\nasflags=[]\n'
                '[build.unit_cflags]\nalpha=["-G0"]\n'
            )
            variants = [[], ["-O1"], ["-ffast-math", "-fno-delayed-branch"], ["-unsupported"]]
            config.write_text(build_config)
            registry = root / "compilers.toml"
            registry.write_text('[compilers."ido-7.1"]\nflag_variants=' + json.dumps(variants) + "\n")
            original = config.read_bytes(), source.read_bytes(), project.compilers.copy()
            compiled: list[tuple[str, tuple[str, ...]]] = []

            def compile_source(project: object, policy: object, source: Path, version: str, out: Path) -> Path:
                from unbake.config import Project

                effective = makefile.flags(cast(Project, project), version, source)
                self.assertIn("-G0", effective)
                compiled.append((version, effective))
                if "-unsupported" in effective:
                    raise Held("compile", "unsupported compiler option")
                matched = "-ffast-math" in effective if version == "us" else "-O1" in effective
                shutil.copyfile(
                    assemble(
                        out.parent,
                        "compiled",
                        assembly("alpha", [0x24020001 if matched else 0x24020002, 0x03E00008, 0]),
                    ),
                    out,
                )
                return out

            with (
                patch("unbake.project.toolchain.REGISTRY_PATH", registry),
                patch.object(build, "compile_object", side_effect=compile_source),
                patch("unbake.decomp.trial.annotate_divergence") as diagnostic,
                redirect_stdout(io.StringIO()),
            ):
                result = try_draft(project, cast(Policy, policy), source, root / "scratch", flags=True)
            self.assertEqual(len(compiled), len(variants) * 2)
            diagnostic.assert_not_called()
            self.assertFalse(result.identical_everywhere)
            self.assertTrue(all(c.match_percent < 100 for c in result.compares.values()))
            report = render(result)
            self.assertIn("VERSION us flags 1: -ffast-math -fno-delayed-branch; objdiff 100.000000%; BEATS", report)
            self.assertIn("VERSION eu flags 1: -O1; objdiff 100.000000%; BEATS", report)
            self.assertIn("compile failed: unsupported compiler option", report)
            self.assertEqual(original, (config.read_bytes(), source.read_bytes(), project.compilers))
            self.assertIn("--flags", result.next_command)

    def test_symbol_names_are_differences_without_linking_or_symbol_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = test_policy(root)
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "jal {callee}\nnop\nlui $v0,%hi({data})\nlw $v0,%lo({data})($v0)\njr $ra\nnop\n"
                ".size alpha,.-alpha\n"
            )
            target = assemble(root, "target", body.format(callee="callee", data="global_data"))
            for changed in (False, True):
                with self.subTest(changed=changed):
                    candidate = assemble(
                        root, "draft", body.format(callee="other" if changed else "callee", data="global_data")
                    )
                    document = diff(policy, "us", "alpha", target, candidate, root / "diff.json")
                    result = compare_object("us", document, "alpha")
                    self.assertEqual(set(result.typed), set(TYPES))
                    self.assertEqual(result.typed["relocation"], int(changed))
                    self.assertEqual(result.identical, 5 if changed else 6)
                    if changed:
                        self.assertIn("callee", result.lines[2])
                        self.assertIn("other", result.lines[2])
                    else:
                        self.assertEqual(result.match_percent, 100)

    def test_compile_wrapper_all_versions_and_missing_target_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy, source = fixture(root, versions=("us", "eu"), case=self)
            content = "#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n"
            source.write_text(content)
            compiled = []

            def compile_source(project: object, policy: object, source: Path, version: str, out: Path) -> Path:
                self.assertTrue(source.read_text().startswith("#define NON_MATCHING 1\n#line 1 "))
                compiled.append(version)
                shutil.copyfile(assemble(out.parent, "compiled", assembly("alpha", [0x24020001, 0x03E00008, 0])), out)
                return out

            with patch.object(build, "compile_object", side_effect=compile_source), redirect_stdout(io.StringIO()):
                result = try_draft(project, cast(Policy, policy), source, root / "scratch")
            # Versions compile concurrently; results are keyed by version, call order is not a contract.
            self.assertCountEqual(compiled, ["us", "eu"])
            self.assertTrue(result.identical_everywhere)
            self.assertEqual(source.read_text(), content)
            self.assertFalse(list((root / "scratch").rglob("*.elf")))
            self.assertFalse(list((root / "scratch").rglob("*.bin")))

    def test_matched_unit_selects_relocatable_input_from_linker_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, _policy, _source = fixture(root, case=self)
            generation = build.current_generation(project, "us")
            configured = project.version("us")
            configured.split.write_text(configured.split.read_text().replace("asm, nonmatchings/alpha", "c, shared"))
            original = assemble(root, "original", assembly("alpha", [0x24020001, 0x03E00008, 0]))
            actual = generation / "obj/src/shared.o"
            actual.parent.mkdir(parents=True)
            shutil.copyfile(original, actual)
            (generation / "objdiff.json").write_text(
                json.dumps({"units": [{"name": "alpha", "target_path": "wrapper.o", "metadata": {"complete": True}}]})
            )
            (generation / "fixture.ld").write_text("SECTIONS { .text : { obj/src/shared.o(.text) } }\n")
            self.assertEqual(target_object(project, "alpha", "us"), actual.resolve())

    def test_register_and_instruction_edits_feed_allocator_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = test_policy(root)
            target = assemble(root, "target", assembly("alpha", [0x8E020018, 0x03E00008, 0]))
            for words, kind in (
                ([0x8E620018, 0x03E00008, 0], "register"),
                ([0x8E02001C, 0x03E00008, 0], "immediate"),
                ([0x24020001, 0x03E00008, 0], "changed"),
                ([0x8E020018, 0x24030002, 0x03E00008, 0], "inserted"),
            ):
                with self.subTest(kind=kind):
                    candidate = assemble(root, "draft", assembly("alpha", words))
                    result = compare_object(
                        "us", diff(policy, "us", "alpha", target, candidate, root / "diff.json"), "alpha"
                    )
                    self.assertGreater(result.typed[kind], 0)
                    if kind == "register":
                        self.assertEqual(result.register_changes, ((0, 0, 16, 19),))
