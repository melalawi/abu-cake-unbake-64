"""Actual producer expressions, strict immutable currentness and bounded public admission."""

import copy
import hashlib
from unittest.mock import patch

import toml

from tests.kit import TempCase
from tests.project_fixture import make
from tests.test_resource_build import ResourceBuildTests
from tests.unified.support import B, gcc_project, phases, retained
from unbake import buildfiles, extract, inputs, land
from unbake.compilers import registry
from unbake.config import Held
from unbake.project import publication_push
from unbake.report import data, producer_recipe


class ProducerTests(TempCase):
    def test_actual_original_and_refreshed_data_recipe_are_equal_without_macro_alias(self):
        original = retained("rw-original.Makefile").decode()
        refreshed = retained("rw-refresh.Makefile").decode()
        leaves = {
            "VER": "us-rev1",
            "*F": "sn64_intelligence_type_records",
            "NAME": "ragewars",
            "KIND": "gnu",
            "us-rev1.D.sn64_intelligence_type_records.SECTION": "",
            "TOOLCHAIN": "$(TOOLCHAIN)",
            "COMPILE_gnu": "$(COMPILE_gnu)",
            "PREPROCESS_gnu": "$(PREPROCESS_gnu)",
        }
        for root in ("UNIT_KEY", "DATA_BIN"):
            self.assertEqual(
                producer_recipe.body(original, root, leaves), producer_recipe.body(refreshed, root, leaves)
            )
        self.assertNotEqual(
            hashlib.sha256(original.encode()).hexdigest(), hashlib.sha256(refreshed.encode()).hexdigest()
        )
        changed = original.replace("--only-section=.data", "--only-section=.rodata")
        self.assertNotEqual(
            producer_recipe.body(original, "DATA_BIN", leaves), producer_recipe.body(changed, "DATA_BIN", leaves)
        )
        for counterfactual in ("A = $(B)\nB = $(A)\n", "A = $(eval unsafe)\n", "A = x\nA = y\n"):
            with self.subTest(counterfactual=counterfactual), self.assertRaises(ValueError):
                producer_recipe.body(counterfactual, "A", {})

    def prepare_dependencies(self):
        project, host, _file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")
        unit = buildfiles.Unit("sn64_intelligence_type_records", 0x80201000, 0x40, 4, "data")
        source = project.src / (unit.name + ".c")
        source.write_text("const int values[1] = { 7 };\n")
        project.version("us").split.write_text(
            "segments:\n  - name: data\n    type: data\n    start: 0x40\n    vram: 0x80201000\n"
            f"    subsegments:\n      - [0x40, data, src/{unit.name}.c]\n  - [0x44]\n"
        )
        for name in data.required(project, "us"):
            path = project.root / name
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("retained boundary input\n")
        (project.root / "Makefile").write_bytes(retained("rw-original.Makefile"))
        binding = data.producer_binding(project, "us", unit)
        values = {
            "version": "us",
            "producer_binding": binding,
            "producer_configuration": data.producer_configuration(project, "us", binding),
            "dependencies_unknown": False,
        }
        certificate = data.producer_recipe_content(
            project.name,
            values,
            (project.root / "Makefile").read_text(),
            hashlib.sha256(retained("rw-original.Makefile")).hexdigest(),
        )
        self.assertIsNotNone(certificate)
        values["producer_recipe_inputs"] = certificate
        deps = inputs.DependencySet(
            tuple(
                inputs.file_pin(project.root / name, root=project.root, root_id="project", reuse=False)
                for name in sorted(data.required(project, "us"))
            ),
            values,
            {"producer": "a" * 64},
        )
        return project, host, unit, {"dependencies": deps.document()}

    def test_public_currentness_preserves_original_proof_across_unused_make_metadata_but_refuses_used_change(self):
        project, _host, _unit, event = self.prepare_dependencies()
        before = copy.deepcopy(event)
        with patch.object(registry, "verify"):
            self.assertIsNone(data.current_dependencies(project, event, "us"))
            make = project.root / "Makefile"
            make.write_bytes(retained("rw-refresh.Makefile"))
            self.assertIsNone(data.current_dependencies(project, event, "us"))
            self.assertEqual(event, before)
            make.write_text(make.read_text() + "\nUNUSED_METADATA = updated\n")
            self.assertIsNone(data.current_dependencies(project, event, "us"))
            # The linker banner was only a cache-envelope salt for this plain
            # DATA command. Old proof identity stays intact after an ordinary
            # pin bump; no 0.3.2 output or current binary identity is fabricated.
            (project.tools / "n64link.version").write_text("n64link 0.3.2 (SN ASN64 2.81 rules)\n")
            self.assertIsNone(data.current_dependencies(project, event, "us"))
            make.write_text(make.read_text().replace("--only-section=.data", "--only-section=.rodata"))
            self.assertEqual(data.current_dependencies(project, event, "us"), "data.producing_recipe.changed")
        self.assertEqual(event, before)

    def test_historical_key_inputs_require_actual_hash_backed_blobs_and_keep_old_linker_identity(self):
        project, _host = make(self.root, versions=("us",))
        name = "tools/n64link.version"
        old = b"n64link 0.3.1 (SN ASN64 2.81 rules)\n"
        (project.root / name).write_bytes(b"n64link 0.3.2 (SN ASN64 2.81 rules)\n")
        recipe = "cc1 -G0 unit.i && as -G0 unit.s"
        expanded = b"const int values[1] = {7};\n"
        salt = hashlib.sha1(old).hexdigest()
        key = hashlib.sha1((salt + " " + recipe + "\n").encode()).hexdigest() + hashlib.sha1(expanded).hexdigest()
        head = "a" * 40
        with patch("unbake.process.run_tool", return_value=old.decode()) as readback:
            proof = data.producing_key_inputs(project, (name,), recipe, expanded, key, head)
        readback.assert_called_once_with(["git", "show", head + ":" + name], project.root, "publish")
        self.assertEqual(proof["origin"], "accepted_commit")
        self.assertEqual(proof["files"][name]["sha256"], hashlib.sha256(old).hexdigest())
        self.assertEqual(bytes.fromhex(proof["files"][name]["content_hex"]), old)
        self.assertEqual(proof["compiler_key"], key)
        with (
            patch("unbake.process.run_tool", return_value="unproved content"),
            self.assertRaisesRegex(Held, "data.provenance.key"),
        ):
            data.producing_key_inputs(project, (name,), recipe, expanded, key, head)

    def test_key_salt_role_never_waives_an_executed_helper_or_source_dependency(self):
        text = "TOOLCHAIN := $(firstword $(shell cat tools/n64link.version tools/producer.py src/unit.c | sha1sum))\n"
        used = {
            "UNIT_KEY": "$(TOOLCHAIN)",
            "DATA_BIN": "link data.o",
            "COMPILE_gnu": "cc1 src/unit.c && python tools/producer.py",
        }
        self.assertEqual(producer_recipe.key_salts(text, used), ("tools/n64link.version",))
        used["DATA_BIN"] = "native-driver --identity tools/n64link.version data.o"
        self.assertEqual(producer_recipe.key_salts(text, used), ())

    def test_compiler_and_assembler_exclusive_groups_are_independent_native_commands(self):
        actual = ["cc1", "-G0", "-G8", "unit.i", "&&", "as", "-G0", "unit.s"]
        self.assertEqual(producer_recipe.argv_identity(actual), (("cc1", "-G8", "unit.i"), ("as", "-G0", "unit.s")))
        changed = producer_recipe.argv_identity(["cc1", "-G8", "unit.i", "&&", "as", "-G8", "unit.s"])
        self.assertNotEqual(changed, producer_recipe.argv_identity(actual))


class ResourceQualificationTests(ResourceBuildTests):
    def test_real_foreign_resource_extraction_projects_only_bounded_storage_to_builtin_bin(self):
        source = self.project.version("us").split.read_bytes()
        projected = extract.splat_rows(self.project, "us")
        self.assertIn("[0xDD840, bin, resident]", projected)
        self.assertNotIn("type: resource", projected)
        self.assertNotIn("execution_vram", projected)
        self.assertEqual(self.project.version("us").split.read_bytes(), source)
        resource = next(iter(buildfiles.resource_bindings(self.project, "us")))
        self.assertEqual(resource.execution_address, 0x04001000)
        self.assertEqual(resource.address, 0x800DCC40)

    def test_real_resource_missing_historical_input_identity_is_named_refusal_not_fabricated_pin(self):
        resource = next(iter(buildfiles.resource_bindings(self.project, "us")))
        receipt = {
            "head": "a" * 40,
            "check": {
                "head": "a" * 40,
                "ok": True,
                "source_findings": [],
                "scopes": [
                    {
                        "resource": resource.name,
                        "version": "us",
                        "rom_start": resource.start,
                        "bytes": resource.size,
                        "execution_vma": resource.execution_address,
                        "resident_lma": resource.address,
                    }
                ],
            },
        }
        with (
            patch("unbake.process.run_tool", return_value=self.source.read_text()),
            self.assertRaisesRegex(Held, "data.provenance.native_chain"),
        ):
            data.retained_producer_inputs(self.project, "us", resource, self.native.read_bytes(), receipt)
        self.assertNotIn("native_data", receipt["check"])


class PublicationTests(TempCase):
    def prepare(self):
        project, host, file = gcc_project(self.root)
        before = project.version("us").split.read_text()
        project.version("us").split.write_text(before.replace(", asm,", ", c,"))
        native = project.build_link("us") / "units" / (B + ".bin")
        native.parent.mkdir(parents=True)
        native.write_bytes(retained("bt-exact.bin"))
        return project, host, file, before

    def test_unchanged_actual_bt_c_newly_bound_to_holding_version_is_selected_once(self):
        project, host, _file, before = self.prepare()

        def git(p, *args):
            if args[:2] == ("diff", "--name-only"):
                return "versions/us/game.yaml\0"
            if args[0] == "show":
                return before
            return ""

        with patch.object(publication_push, "_git", side_effect=git):
            result = publication_push.admission(project, host, "a" * 40, "b" * 40)
        self.assertEqual(result["work"]["functions_compared"], 1)
        self.assertEqual(result["work"]["native_bytes_read"], 28652)
        self.assertTrue(result["ok"])

    def test_unrelated_recipe_and_symbol_additions_do_not_select_all_exact_holders(self):
        project, host, _file, _before = self.prepare()
        oldconfig = (project.root / "config.toml").read_text()
        values = toml.loads(oldconfig)
        values["units"] = {"src/unrelated.c": {"compiler": "ido-7.1", "options": phases(compile=["-O1"])}}
        (project.root / "config.toml").write_text(toml.dumps(values))
        oldsymbol = project.version("us").symbols.read_text()
        project.version("us").symbols.write_text(oldsymbol + "unrelated_symbol = 0x80001000;\n")

        def git(p, *args):
            if args[:2] == ("diff", "--name-only"):
                return "config.toml\0versions/us/symbol_addrs.txt\0"
            if args[0] == "show":
                return oldconfig if args[-1].endswith(":config.toml") else oldsymbol
            return ""

        # The recipe/config identity for this case is an unchanged ready GCC view.
        # Replay the prior parsed view at config load; the added unrelated IDO TU
        # changes no used dependency or recipe of the real BT holder.
        with (
            patch.object(publication_push, "_git", side_effect=git),
            patch.object(publication_push.config, "load", return_value=project),
        ):
            result = publication_push.admission(project, host, "a" * 40, "b" * 40)
        self.assertEqual(result["work"]["functions_compared"], 0)
        self.assertEqual(result["scopes"], [])

    def test_persistent_publication_manifest_includes_unchanged_symbols_headers_helpers_and_build_recipe(self):
        project, host, file, _before = self.prepare()
        file.write_text('#include "types.h"\nint ' + B + "(void) { return 1; }\n")
        names = (
            "Makefile",
            "units.mk",
            "tools/n64link.version",
            "tools/compiler-driver.sha256",
            "tools/compiler_contracts.py",
            "tools/recipe_options.py",
            "versions/us/slices.mk",
            "versions/us/symbols.ld",
            "versions/us/fixture.ld",
            "versions/us/fixture.data.ld",
        )
        for name in names:
            path = project.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("retained boundary persistent input\n")
        receipt = land._receipt(project, host, B, file, ("us",), set())
        manifest = {row["path"]: row for row in receipt["dependency_manifest"]}
        expected = {
            *names,
            "config.toml",
            "layout.toml",
            "include/types.h",
            "versions/us/game.yaml",
            "versions/us/symbol_addrs.txt",
            "src/" + B + ".c",
            "tools/compilers.sha256",
        }
        self.assertTrue(expected <= manifest.keys())
        self.assertTrue(
            all(
                manifest[name]["digest"] == hashlib.sha256((project.root / name).read_bytes()).hexdigest()
                for name in expected
            )
        )
        self.assertEqual(manifest["src/" + B + ".c"]["role"], "source")
        (project.root / "tools/recipe_options.py").unlink()
        with self.assertRaisesRegex(Held, "land.dependencies"):
            land._receipt(project, host, B, file, ("us",), set())
