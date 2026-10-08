"""Retained RW constant input/key/CAS/ELF bytes, with only the CPP boundary mocked."""

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import toml

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config, inputs, process, runner
from unbake.compilers.drivers import Tools
from unbake.compilers.recipe_options import UnitRecipe
from unbake.compilers.registry import specification
from unbake.project import publication_push
from unbake.report import data, producer_recipe
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/immutable_data"


class ImmutableDataTests(ProjectCase):
    versions = ("us-rev1",)

    def setUp(self):
        super().setUp()
        self.prepare(FIXTURE)

    def prepare(self, fixture):
        self.fixture = fixture
        self.proof = json.loads((fixture / "provenance.json").read_text())
        for path in self.project.src.glob("*.c"):
            path.unlink()
        if (fixture.parent / "headers").is_dir():
            shutil.copytree(fixture.parent / "headers", self.project.root, dirs_exist_ok=True)
        for name, digest in self.proof["files"].items():
            self.assertEqual(hashlib.sha256((self.fixture / name).read_bytes()).hexdigest(), digest)
        self.name = self.proof["unit"]
        self.source = self.project.src / (self.name + ".c")
        self.source.write_bytes((self.fixture / "source.c").read_bytes())
        self.expanded = (self.fixture / "input.i").read_text()
        meta = self.project.version("us-rev1")
        start, end = self.proof["scope"]
        meta.split.write_text(
            "name: fixture\nsegments:\n  - [0x0, header, header]\n"
            f"  - name: constants\n    type: code\n    start: 0x{start:X}\n"
            f"    vram: 0x{self.proof['address']:X}\n    subsegments:\n"
            f'      - [0x{start:X}, data, "src/{self.name}.c"]\n  - [0x{end:X}]\n'
        )
        image = bytearray(end)
        image[start:end] = (self.fixture / "native.bin").read_bytes()
        meta.baserom.write_bytes(image)
        values = toml.load(self.project.root / "config.toml")
        values["project"]["default_compiler"] = self.proof["compiler"]
        values["compilers"] = {self.proof["compiler"]: {"cflags": self.proof["cflags"]}}
        values["build"]["cppflags"] = self.proof["cppflags"]
        values["build"]["asflags"] = ["-march=vr4300", "-mabi=32", "-EB", "--no-pad-sections"]
        values["build"]["gnu_asflags"] = self.proof["sn64_asflags"]
        values["version"]["us-rev1"]["baserom_sha1"] = hashlib.sha1(image).hexdigest()
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)
        for name in ("compilers.sha256", "n64link.version"):
            shutil.copyfile(self.fixture / name, self.project.tools / name)
        # The retained key predates the driver identity pin: an empty file adds no bytes to the toolchain hash.
        (self.project.tools / "compiler-driver.sha256").write_bytes(b"")
        self.makefile = buildfiles.makefile(self.project, self.host)
        (self.project.root / "Makefile").write_text(self.makefile)
        (self.project.root / "units.mk").write_text(buildfiles.units_mk(self.project))
        (meta.split.parent / "fixture.data.ld").write_text(buildfiles.data_link_script(self.project, "us-rev1"))
        for name in ("fixture.ld", "symbols.ld"):
            (meta.split.parent / name).write_text("SECTIONS {}\n" if name == "fixture.ld" else "")
        self.unit = next(iter(buildfiles.data_bindings(self.project, "us-rev1")))
        self.native = self.project.build_link("us-rev1") / "data" / (self.name + ".bin")
        self.native.parent.mkdir(parents=True, exist_ok=True)
        for src, suffix in (("native.bin", ".bin"), ("final.elf", ".elf"), ("linked.o", ".o")):
            shutil.copyfile(self.fixture / src, self.native.with_suffix(suffix))
        build_src = self.project.build_link("us-rev1") / "src"
        build_src.mkdir(parents=True, exist_ok=True)
        for src, suffix in (("input.i", ".i"),):
            shutil.copyfile(self.fixture / src, build_src / (self.name + suffix))
        # The retained key names the old effective recipe; the current producing recipe names its key the same way.
        recipe = buildfiles.native_compile_recipe(self.project, self.name, Tools(**self.proof["tools"]))
        prefix = hashlib.sha1(
            b"".join((self.project.root / n).read_bytes() for n in producer_recipe.toolchain_files(self.makefile))
        ).hexdigest()
        key = (
            hashlib.sha1((prefix + " " + recipe + "\n").encode()).hexdigest()
            + hashlib.sha1(self.expanded.encode()).hexdigest()
        )
        (build_src / (self.name + ".key")).write_text(key + "\n")
        self.cas = self.project.root / "build/cas" / (key + ".o")
        self.cas.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.fixture / "original.o", self.cas)
        # An ordinary unchanged Git materialization creates the newer source.
        # No timestamp manipulation is used even in the regression case.
        self.source.write_bytes(self.source.read_bytes())
        self.assertGreater(self.source.stat().st_mtime_ns, self.native.stat().st_mtime_ns)
        if (fixture / "symbols.ld").is_file():
            shutil.copyfile(fixture / "symbols.ld", meta.split.parent / "symbols.ld")
        # The compiler binary is not shipped: a placeholder file stands at the Make CC path and its digest
        # answers the registry pin, so the pin comparison itself still runs.
        compiler = self.project.compiler_for(f"src/{self.name}.c")
        cc = self.project.root / buildfiles.relative(self.project, compiler.cc)
        cc.parent.mkdir(parents=True, exist_ok=True)
        cc.write_bytes(b"compiler")
        spec = specification(compiler.id)
        real_digest = inputs.digest

        def digest(path, **kwargs):
            return spec.pins[spec.cc] if Path(path) == cc else real_digest(path, **kwargs)

        patcher = patch.object(inputs, "digest", side_effect=digest)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = Tools(**self.proof["tools"])
        self.bytes = self.native.read_bytes()
        self.receipt = {
            "head": self.proof["successful_push_sha"],
            "check": {
                "head": self.proof["successful_push_sha"],
                "ok": True,
                "source_findings": [],
                "scopes": [
                    {"data": self.name, "version": "us-rev1", "rom_start": self.unit.start, "bytes": self.unit.size}
                ],
            },
        }

    def assertUnqualified(self, call):
        """A refusal is a typed Held from the strict native proof or an absent proof; never an admitted proof."""
        try:
            result = call()
        except config.Held:
            return
        self.assertIsNone(result[0] if isinstance(result, tuple) else result)

    def capture(self, *, current=None, tools=True):
        with (
            patch.object(runner, "preprocess", return_value=self.expanded if current is None else current) as cpp,
            patch.object(process, "run_tool", return_value=(self.fixture / "source.c").read_text()),
        ):
            result = data.capture_producer(
                self.project,
                "us-rev1",
                self.unit,
                self.bytes,
                self.bytes,
                host=self.host,
                producer_tools=self.tools if tools else None,
                publication_receipt=self.receipt,
            )
        return result, cpp

    def test_actual_unchanged_git_materialization_admits_content_chain_and_portable_proof(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.source, self.native, self.cas)}
        with patch.object(process, "run_native") as native:
            proof, cpp = self.capture()
        self.assertIsNotNone(proof)
        cpp.assert_called_once_with(self.project, self.host, self.source, "us-rev1", unit=self.name)
        native.assert_not_called()
        # Only the immutable selected Git source is read; native remains mocked.
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assertEqual(
            [(e["rom_offset"], e["size"]) for e in proof["payload"]["evidence"][0]["extents"]],
            [(self.proof["scope"][0], 4)],
        )
        certificate = proof["dependencies"]["values"]["producer_native_inputs"]
        self.assertEqual(certificate["compiler_key"], self.cas.stem)
        self.assertEqual(certificate["original_object_sha256"], self.proof["files"]["original.o"])
        events = data.record_producers(self.project, [proof])
        self.assertEqual(len(events), 1)
        self.assertEqual(data.record_producers(self.project, [proof]), [])
        record = attempts.ledger(self.project).latest("native.data", proof["subject"])
        self.assertIsNone(data.current_dependencies(self.project, record, "us-rev1"))
        shutil.rmtree(self.project.root / "build")
        self.assertIsNone(data.current_dependencies(self.project, record, "us-rev1"))

    def test_actual_changed_source_header_cpp_and_missing_provenance_remain_unqualified(self):
        source = self.source.read_bytes()
        self.source.write_bytes(source.replace(b"122.87999725341797f", b"1.0f"))
        self.assertUnqualified(lambda: self.capture(current=self.expanded.replace("122.87999725341797f", "1.0f")))
        self.source.write_bytes(source)
        # A used header/type or CPP definition changes the current preprocessing.
        for current in (self.expanded.replace("float", "double"), self.expanded + "int changed;\n"):
            self.assertUnqualified(lambda current=current: self.capture(current=current))
        self.assertUnqualified(lambda: self.capture(tools=False))
        for path in (
            self.cas,
            self.native.with_suffix(".o"),
            self.project.build_link("us-rev1") / "src" / (self.name + ".i"),
        ):
            raw = path.read_bytes()
            path.unlink()
            self.assertUnqualified(lambda: self.capture())
            path.write_bytes(raw)
        with (
            patch.object(process, "run_tool", return_value=(self.fixture / "source.c").read_text()),
            patch.object(
                runner,
                "preprocess",
                side_effect=config.Held(process.named("native.cpp", "CPP refused", owner="runner", stage="compile")),
            ),
        ):
            self.assertUnqualified(
                lambda: data.capture_producer(
                    self.project,
                    "us-rev1",
                    self.unit,
                    self.bytes,
                    self.bytes,
                    host=self.host,
                    producer_tools=self.tools,
                )
            )

    def test_actual_changed_flags_recipe_binding_and_native_corruption_refuse(self):
        original = self.project
        key = self.project.unit_path(self.source)
        recipe = self.project.units.get(key, UnitRecipe(self.project.default_compiler))
        options = tuple((phase, ("-O1",) if phase == "compile" else values) for phase, values in recipe.options)
        self.project = replace(original, units={**original.units, key: replace(recipe, options=options)})
        self.assertUnqualified(lambda: self.capture())
        self.project = original
        for path, edit in (
            (self.project.root / "Makefile", lambda b: b.replace(b"-quiet", b"-quiet -O1")),
            (
                self.project.root / "units.mk",
                lambda b: b.replace(b"PREPROCESS_FLAGS =", b"CPPFLAGS = -Dfloat=double\n#"),
            ),
            (self.project.tools / "compilers.sha256", lambda b: b + b"changed pin\n"),
            (self.cas, lambda b: b[:-1] + bytes([b[-1] ^ 1])),
            (self.native.with_suffix(".o"), lambda b: b[:-1] + bytes([b[-1] ^ 1])),
            (self.native.with_suffix(".elf"), lambda b: b.replace(self.bytes, bytes(4), 1)),
        ):
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                path.write_bytes(edit(raw))
                # The real preprocessor is mocked: a changed unit preprocessing flag reaches it as changed output.
                current = self.expanded.replace("float", "double") if path.name == "units.mk" else None
                self.assertUnqualified(lambda current=current: self.capture(current=current))
                path.write_bytes(raw)
        meta = self.project.version("us-rev1")
        meta.split.write_text(
            meta.split.read_text().replace(f"0x{self.unit.address:X}", f"0x{self.unit.address + 4:X}")
        )
        wrong = next(iter(buildfiles.data_bindings(self.project, "us-rev1")))
        with (
            patch.object(process, "run_tool", return_value=(self.fixture / "source.c").read_text()),
            patch.object(runner, "preprocess", return_value=self.expanded),
        ):
            self.assertUnqualified(
                lambda: data.capture_producer(
                    self.project,
                    "us-rev1",
                    wrong,
                    self.bytes,
                    self.bytes,
                    host=self.host,
                    producer_tools=self.tools,
                    publication_receipt=self.receipt,
                )
            )

    def test_existing_selected_producer_sources_route_uses_same_admission_and_hygiene(self):
        with (
            patch.object(publication_push, "_git", return_value=""),
            patch.object(runner, "preprocess", return_value=self.expanded) as cpp,
            patch.object(process, "run_tool", return_value=(self.fixture / "source.c").read_text()),
        ):
            checked = publication_push.admission(
                self.project,
                self.host,
                "head",
                "head",
                producer_sources=(self.source,),
                producer_tools=self.tools,
                producer_receipts=(self.receipt,),
            )
        self.assertEqual(checked["work"]["native_bytes_read"], 4)
        self.assertEqual(checked["work"]["data_extents_compared"], 1)
        self.assertEqual(len(checked["native_data"]), 1)
        cpp.assert_called_once()
        self.source.write_text(self.source.read_text() + '\nvoid bad(void) { __asm__("nop"); }\n')
        with patch.object(publication_push, "_git", return_value=""), self.assertRaises(config.Held) as held:
            publication_push.admission(
                self.project,
                self.host,
                "head",
                "head",
                producer_sources=(self.source,),
                producer_tools=self.tools,
                producer_receipts=(self.receipt,),
            )
        self.assertEqual(held.exception.key, "publish.push_rules")

    def test_actual_residual_used_relocations_and_own_provides_capture_without_native_replay(self):
        from unbake.objects.elf import Object

        captured = {}
        for fixture in sorted((FIXTURE / "residual").glob("*/provenance.json")):
            with self.subTest(unit=fixture.parent.name):
                self.prepare(fixture.parent)
                obj = Object(self.cas)
                self.assertEqual(
                    sum(len(obj.relocations(i)) for i in range(len(obj.sections))), self.proof["relocations"]
                )
                current = self.expanded
                if self.name.startswith("resident_menu_"):
                    current = current.replace("extern float D_800C4E04_de;", "extern const float D_800C4E04_de;")
                    self.assertNotEqual(current, self.expanded)
                with patch.object(process, "run_native") as native:
                    proof, cpp = self.capture(current=current)
                self.assertIsNotNone(proof)
                cpp.assert_called_once()
                native.assert_not_called()
                self.assertIn("producer_native_inputs", proof["dependencies"]["values"])
                captured[self.name] = sum(e["size"] for e in proof["payload"]["evidence"][0]["extents"])
        self.assertEqual(sum(v for k, v in captured.items() if k.startswith("resident_menu_")), 14540)
        self.assertEqual(sum(v for k, v in captured.items() if k.startswith("ragewars_")), 1620)
        self.assertEqual(captured["ragewars_menu_sexysteve_token_us_rev1"], 20)

    def test_actual_used_referent_type_and_unused_extern_boundaries_refuse_changes(self):
        fixture = FIXTURE / "residual/ragewars_pickup_powerups_us_rev1"
        self.prepare(fixture)
        self.assertIsNotNone(self.capture()[0])
        path = self.project.root / "versions/us-rev1/symbols.ld"
        raw = path.read_text()
        line = raw.splitlines()[0]
        address = int(line.split(" = ")[1].split(")")[0])
        path.write_text(raw.replace(str(address), str(address + 4), 1))
        self.assertUnqualified(lambda: self.capture())
        path.write_text(raw)
        self.assertUnqualified(lambda: self.capture(current=self.expanded.replace("unsigned short", "unsigned int", 1)))
        from unbake.objects.elf import Object

        obj = Object(self.cas)
        unused = "extern float unused_symbol;\n"
        self.assertTrue(data._same_preprocessed_inputs(unused, unused.replace("float", "const float"), obj))
        for old, new in (
            (
                unused + "int value = sizeof(unused_symbol);\n",
                unused.replace("float", "const float") + "int value = sizeof(unused_symbol);\n",
            ),
            (unused, unused.replace("float", "double")),
            (unused, unused.replace("float", "volatile float")),
        ):
            self.assertFalse(data._same_preprocessed_inputs(old, new, obj))
