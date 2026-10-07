"""Whole native code/literal proof and effective bindings from real RW route9 slices."""

import json
import struct
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles, cache
from unbake.compilers import registry
from unbake.compilers.families import family_for
from unbake.config import Compiler, Held

FIXTURES = Path(__file__).parent / "fixtures" / "runtime"
NATIVE = json.loads((FIXTURES / "native.json").read_text())


class RuntimeHelperTests(ProjectCase):
    versions = ("us",)

    def payload(self, version):
        native = NATIVE[version]
        body = (FIXTURES / f"{version}-converter.bin").read_bytes()
        values = (FIXTURES / f"{version}-constants.bin").read_bytes()
        literals = {
            int(address, 16): values[i * 8 : (i + 1) * 8] for i, address in enumerate(native["constant_addresses"])
        }
        return native, body, literals

    def project_with_helper(self, version):
        native, body, literals = self.payload(version)
        spec = registry.specification("gcc-2.8.1-sn64")
        compiler = Compiler(
            spec.id,
            spec.kind,
            self.project.tools / spec.id / spec.cc,
            Path(spec.as_),
            spec.cflags,
            self.project.tools / "compilers.sha256",
        )
        self.project = replace(self.project, compilers={compiler.id: compiler}, default_compiler=compiler.id)
        version_config = self.project.version("us")
        data = bytes(0x40) + body + b"".join(literals.values())
        version_config.baserom.write_bytes(data)
        end = 0x40 + len(body)
        version_config.split.write_text(
            f"segments:\n"
            f"  - name: code\n"
            f"    type: code\n"
            f"    start: 0x40\n"
            f"    vram: {native['entry']}\n"
            f"    subsegments:\n"
            f"      - [0x40, data, opaque]\n"
            f"  - name: literals\n"
            f"    type: code\n"
            f"    start: 0x{end:X}\n"
            f"    vram: {native['constant_addresses'][0]}\n"
            f"    subsegments:\n"
            f"      - [0x{end:X}, rodata, literals]\n"
            f"  - [0x{len(data):X}]\n"
            f""
        )
        version_config.symbols.write_text("")
        cache.forget()
        return native, body, literals

    def test_real_native_helpers_reach_generated_linker_options(self):
        for version in NATIVE:
            with self.subTest(version=version):
                native, _, _ = self.project_with_helper(version)
                self.assertIn(
                    f"PROVIDE(__floatdidf = 0x{int(native['entry'], 16):08X});",
                    buildfiles.symbols_ld(self.project, "us"),
                )

    def test_proved_runtime_alias_reaches_native_link_command_without_rebuilding(self):
        from types import SimpleNamespace

        from unbake import runner

        native, body, _ = self.project_with_helper("eu-x")
        version_dir = self.project.root / "versions" / "us"
        (version_dir / "symbols.ld").write_text("")
        (version_dir / f"{self.project.name}.ld").write_text("SECTIONS {}")
        calls = []

        def native_tool(argv, cwd, phase, **kwargs):
            calls.append(argv)
            if argv[0] == str(self.host.mips_objcopy):
                Path(argv[-1]).write_bytes(body)
            return ""

        work = self.root / "link"
        work.mkdir()
        row = SimpleNamespace(address=int(native["entry"], 16), name="alpha")
        with (
            patch.object(runner, "undefined", return_value={"__floatdidf"}),
            patch("unbake.objects.elf.Object"),
            patch("unbake.objects.rodata.unresolved_sections", return_value=[]),
            patch("unbake.process.run_tool", side_effect=native_tool),
        ):
            result = runner.link(
                self.project, self.host, work / "placed.o", "us", row, work, self.project.src / "alpha.c"
            )
        self.assertEqual(result, body)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], str(self.host.mips_ld))
        self.assertIn(f"--defsym=__floatdidf=0x{row.address:08X}", calls[0])

    def test_complete_code_literals_and_abi_are_required(self):
        for version in NATIVE:
            _native, body, literals = self.payload(version)
            for ident in ("gcc-2.7.2-kmc", "gcc-2.8.1-sn64", "ido-7.1"):
                with self.subTest(version=version, compiler=ident):
                    family = family_for(ident)
                    result = family.runtime_helpers(body, lambda address, size, values=literals: values[address])
                    if ident.startswith("ido"):
                        self.assertEqual(result, ())
                        continue
                    self.assertEqual(len(result), 1)
                    offset, helper = result[0]
                    self.assertEqual((offset, helper.name, helper.size), (0, "__floatdidf", 136))
                    self.assertEqual(
                        (helper.arguments, helper.result), ((("a0/a1", "signed long long"),), ("f0/f1", "double"))
                    )
                    for index in range(34):
                        bad = bytearray(body)
                        bad[index * 4] ^= 1
                        self.assertEqual(
                            family.runtime_helpers(bytes(bad), lambda a, n, values=literals: values[a]), ()
                        )
                    self.assertEqual(family.runtime_helpers(body, lambda a, n: struct.pack(">d", 3.0)), ())
                    self.assertEqual(family.runtime_helpers(body[:-4], lambda a, n, values=literals: values[a]), ())

    def test_bindings_are_unique_and_unchanged_queries_do_no_native_reads(self):
        from unbake.compilers.runtime import bindings

        native, body, literals = self.project_with_helper("eu-x")
        expected = {"__floatdidf": int(native["entry"], 16)}
        self.assertEqual(bindings(self.project, "us"), expected)
        with patch.object(Path, "open", side_effect=AssertionError("repeated native read")):
            for _ in range(20):
                self.assertEqual(bindings(self.project, "us"), expected)
        version = self.project.version("us")
        version.baserom.write_bytes(bytes(0x40) + body + body + b"".join(literals.values()))
        version.split.write_text(
            version.split.read_text()
            .replace("start: 0xC8", "start: 0x150")
            .replace("[0xC8,", "[0x150,")
            .replace("[0xE0]", "[0x168]")
        )
        with self.assertRaisesRegex(Held, "nonunique native helpers"):
            bindings(self.project, "us")
