"""Standalone graph and SN64 helper tests; all scratch stays under TMPDIR."""

import dataclasses
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.support import test_policy, tool, write_policy
from unbake.project import config, makefile

WORK = Path(tempfile.gettempdir())


def helper(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, makefile.TEMPLATES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def executable(path: Path, code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o755)


def fixture(root: Path, kind: str = "ido") -> Any:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    policy_path = write_policy(root)
    patch.dict(os.environ, UNBAKE_POLICY=str(policy_path)).start()
    for directory in ("tools", "include", "src", "versions/us"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "baserom.us.z64").write_bytes(b"ABC")
    (root / "include/value.h").write_text("#define VALUE 1\n")
    (root / "src/middle.c").write_text("B")
    split = root / "versions/us/game.yaml"
    split.write_text(
        "name: game\noptions:\n  base_path: .\nsegments:\n  - name: main\n    t"
        "ype: code\n    start: 0x0\n    vram: 0x80000000\n    subsegments:\n  "
        "    - [0x0, asm, first]\n      - [0x1, c, middle]\n  - [0x2, bin, t"
        "ail]\n  - [0x3]\n"
    )
    (root / "versions/us/symbol_addrs.txt").write_text("first = 0x80000000;\nmiddle = 0x80000001;\n")
    prelude = (
        (
            "from pathlib import Path\nimport sys, json\na = sys.argv[1:]\ndef ar"
            "g(k): return a[a.index(k)+1]\ntrace = Path("
        )
        + repr(str(root / "calls"))
        + ")\ndef record(kind):\n with trace.open('a') as f: f.write(kind + '\\n')\n"
    )
    object_code = (
        "import struct\ndef object_bytes(content):\n names=b'\\0.text\\0.shstr"
        "tab\\0'\n data=bytearray(52); data.extend(content); names_at=len(da"
        "ta); data.extend(names); data.extend(bytes((-len(data))%4)); tabl"
        "e=len(data); data.extend(bytes(40)); data.extend(struct.pack('>10"
        "I',1,1,6,0,52,len(content),0,0,4,0)); data.extend(struct.pack('>1"
        "0I',7,3,0,0,names_at,len(names),0,0,1,0)); data[:52]=b'\\x7fELF\\x0"
        "1\\x02\\x01'+bytes(9)+struct.pack('>HHIIIIIHHHHHH',1,8,1,0,0,table,"
        "0,52,0,0,40,3,2); return bytes(data)\n"
    )
    executable(
        root / "tools/fixture/cc",
        prelude
        + object_code
        + (
            "if '-M' in a:\n print('middle.o: ' + a[-1] + ' include/value.h')\ne"
            "lif '-E' in a:\n print(Path(a[-1]).read_text() + Path('include/val"
            "ue.h').read_text())\nelse:\n record('cc')\n Path(arg('-o')).write_by"
            "tes(object_bytes(b'B'))\n"
        ),
    )
    executable(
        root / "tools/as",
        prelude
        + (
            "record('as')\nPath(arg('-o')).write_bytes(Path(a[-1]).read_bytes()"
            ")\nPath(arg('--MD')).write_text(arg('-o') + ': ' + a[-1] + '\\n')\n"
        ),
    )
    executable(
        root / "tools/ld",
        prelude
        + (
            "import re\nrecord('ld')\nscript = Path(arg('-T')).read_text()\nobjec"
            "ts = re.findall(r'(obj/[^ ()]+\\.o)\\(', script)\ndef payload(p):\n d"
            "ata=Path(p).read_bytes()\n return data[52:53] if data[:4]==b'\\x7fE"
            "LF' else data\nPath(arg('-o')).write_bytes(b''.join(payload(p) for"
            " p in objects))\n"
        ),
    )
    executable(
        root / "tools/objcopy", prelude + "record('objcopy')\nPath(a[-1]).write_bytes(Path(a[-2]).read_bytes())\n"
    )
    splat = prelude + (
        "record('splat')\no = {}\nfor line in Path(a[-1]).read_text().splitl"
        "ines()[1:]:\n k,v = line.strip().split(': ',1); o[k] = json.loads("
        "v)\nasm = Path(o['asm_path']); assets = Path(o['asset_path']); bui"
        "ld = asm.parent\nasm.mkdir(parents=True); assets.mkdir(parents=Tru"
        "e)\n(asm/'first.s').write_bytes(b'A')\n(assets/'tail.bin').write_by"
        "tes(b'C')\nPath(o['ld_script_path']).write_text('SECTIONS { .text "
        ": { ' + str(build/'asm/first.s.o') + '(.text); ' + str(build/'src"
        "/middle.c.o') + '(.text); ' + str(build/'assets/tail.bin.o') + '("
        ".data); } }')\nfor key in ('undefined_funcs_auto_path','undefined_"
        "syms_auto_path'): Path(o[key]).write_text('')\n"
    )
    splat += (
        "dump = Path(o['base_path']) / '.splat' / 'splat_symbols.csv'\n"
        "dump.parent.mkdir(parents=True, exist_ok=True)\n"
        "dump.write_text('name,vram_start\\n')\n"
    )
    executable(root / "tools/splat", splat)
    if kind == "sn64":
        executable(
            root / "tools/fixture/cc",
            prelude
            + (
                "record('cc1')\nPath(arg('-o')).write_text('.section .text\\n.globl "
                "middle\\n.ent middle\\nmiddle:\\n nop\\n.end middle\\n')\n"
            ),
        )
        (root / "tools/fixture/asn64.exe").write_bytes(b"assembler")
        executable(
            root / "tools/fixture/wibo",
            prelude + "record('asn64')\nPath(arg('-o')).write_bytes(Path(a[-1]).read_bytes())\n",
        )
        executable(
            root / "tools/fixture/psyq-obj-parser",
            prelude + "record('parser')\nPath(arg('-o')).write_bytes(Path(a[0]).read_bytes())\n",
        )
    pins = (
        ["tools/fixture/cc", "tools/as"]
        if kind == "ido"
        else ["tools/fixture/cc", "tools/fixture/asn64.exe", "tools/fixture/wibo", "tools/fixture/psyq-obj-parser"]
    )
    if kind == "ido":
        (root / "tools/fixture/as").write_bytes((root / "tools/as").read_bytes())
        (root / "tools/fixture/as").chmod(0o755)
        pins[1] = "tools/fixture/as"
    digests = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in pins}
    (root / "tools/compiler.sha256").write_text(
        "".join(digest + "  " + name + "\n" for name, digest in digests.items())
    )
    registry = root / "registry.toml"
    registry.write_text(
        "[compilers.fixture]\nkind = "
        + json.dumps(kind)
        + "\nfamily = "
        + json.dumps("gcc" if kind == "sn64" else "ido")
        + '\ndecompme = "fixture"\nsource = "supplied"\nhost = "linux-x86_64"\ncc = "cc"\nas = '
        + json.dumps("asn64.exe" if kind == "sn64" else "as")
        + "\ncflags = []\ndrivers = []\n[compilers.fixture.pins]\n"
        + "".join(json.dumps(Path(name).name) + " = " + json.dumps(digest) + "\n" for name, digest in digests.items())
    )
    patch("unbake.project.toolchain.REGISTRY_PATH", registry).start()
    compiler = config.Compiler(
        "fixture",
        kind,
        root / "tools/fixture/cc",
        root / ("tools/fixture/asn64.exe" if kind == "sn64" else "tools/fixture/as"),
        ("-O2",) if kind == "sn64" else (),
        root / "tools/compiler.sha256",
    )
    version = config.Version(
        "us",
        root / "baserom.us.z64",
        hashlib.sha1(b"ABC").hexdigest(),
        split,
        root / "versions/us/symbol_addrs.txt",
        ("VERSION_US=1",),
    )
    project = config.Project(
        root,
        "game",
        "Game",
        "us",
        ("us",),
        root / "src",
        (root / "include",),
        root / "asm",
        root / "tools",
        {"fixture": compiler},
        "fixture",
        {},
        {"us": version},
    )
    facts = {
        "ld": str(root / "tools/ld"),
        "objcopy": str(root / "tools/objcopy"),
        "splat": str(root / "tools/splat"),
        "asflags": [],
        "as": str(root / "tools/as"),
    }
    if kind == "sn64":
        facts.pop("as")
        facts.update(
            assembly_compiler="fixture",
            cpp=tool("cpp"),
            cppflags=["-P", "-undef", "-nostdinc", "-D_LANGUAGE_C", "-D__GNUC__=2"],
            asflags=["-mips3", "-Iinclude"],
            sn64_asflags=["-mips3", "-Iinclude"],
        )
    (root / "config.toml").write_text(
        '[compilers.fixture]\nsupply = "tools/fixture"\n[build]\n'
        + "".join(k + " = " + json.dumps(v) + "\n" for k, v in facts.items())
    )
    return project, SimpleNamespace(cores=2, cache_root=root / "cache")


def write_rendered(project: Any) -> None:
    for name, content in makefile.render(project).items():
        path = project.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    digest = project.version("us").baserom_sha1
    (project.root / "versions/us/game.sha1").write_text(digest + "  build/us/game.us.z64\n")
    (project.root / "versions/us/baserom.sha1").write_text(digest + "  baserom.us.z64\n")


class MakefileTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name)

    def test_missing_host_tool_is_refused_by_name(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        (self.root / "tools/splat").unlink()
        result = subprocess.run(["make", "-C", str(self.root), "VERSION=us", "extract"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tools/splat: missing executable", result.stderr)

    def test_shared_linker_fragment_requires_explicit_facts(self) -> None:
        script = "SECTIONS {\n/DISCARD/ : { *(*) }\n}"
        for section in (".rdata", ".rodata"):
            with self.subTest(section=section):
                result = makefile.linker_script(
                    script, [dict(object="obj/src/middle.o", section=section, address=0x80001000)]
                )
                self.assertIn("(NOLOAD) : SUBALIGN(1)", result)
                self.assertLess(result.index(".resident_"), result.index("/DISCARD/"))
        with self.assertRaisesRegex(config.Held, "rodata.address"):
            makefile.linker_script(script, [dict(object="obj/src/middle.o", section=".rdata")])

    def test_partial_selection_preserves_nested_paths_and_original_rows(self) -> None:
        module = helper("extract")
        (self.root / "src/nested").mkdir(parents=True)
        (self.root / "src/nested/draft.c").write_text("#ifdef NON_MATCHING\nint draft(void) {return 0;}\n#endif\n")
        original = "      - [0x10, asm, nested/draft]\n      - [0x20, asm, untouched]\n"
        self.assertEqual(
            module.partial_rows(original, self.root / "src"),
            '      - [0x10, c, "nested/draft"]\n      - [0x20, asm, untouched]\n',
        )

    def test_partial_link_bypasses_matching_constant_proof(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        build = self.root / "build/us.nonmatching"
        obj = build / "obj/src/draft.o"
        obj.parent.mkdir(parents=True)
        code = (
            ".text\n.set noreorder\nlui $2,%hi(pool)\nlw $2,%lo(pool)($2)\njr $31\n"
            "nop\n.section .rdata\npool: .word 0x3f800000\n"
        )
        subprocess.run(
            [tool("mips-linux-gnu-as"), "-EB", "-o", str(obj)], input=code, text=True, capture_output=True, check=True
        )
        script = build / "input.ld"
        script.write_text("SECTIONS {\n.text : { obj/src/draft.o(.text) }\n/DISCARD/ : { *(*) }\n}\n")
        ranges = build / "ranges.json"
        ranges.write_text(json.dumps({"draft": dict(start=0, end=12, address=0x80000000)}))
        command = [
            sys.executable,
            str(self.root / "tools/layout.py"),
            "--script",
            str(script),
            "--output",
            str(build / "link.ld"),
            "--build",
            str(build),
            "--ranges",
            str(ranges),
            "--baserom",
            str(self.root / "baserom.us.z64"),
        ]
        for partial, expected in (("0", False), ("1", True)):
            with self.subTest(partial=partial):
                result = subprocess.run([*command, "--non-matching", partial], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
        self.assertIn(".partial_draft_rdata", (build / "link.ld").read_text())
        self.assertNotIn("NOLOAD", (build / "link.ld").read_text())

    def test_missing_recipe_key_refused_by_name(self) -> None:
        project, _ = fixture(self.root)
        (self.root / "config.toml").write_text("[build]\n")
        with self.assertRaisesRegex(config.Held, r"\[build\].as"):
            makefile.render(project)

    def test_sn64_recipe_flags_and_helpers(self) -> None:
        project, _ = fixture(self.root, "sn64")
        rendered = makefile.render(project)
        self.assertIn("tools/asn64.py", rendered)
        self.assertIn("tools/resolve_external_branches.py", rendered)
        self.assertEqual(makefile.flags(project, "us", "src/middle.c"), ("-O2", "-Iinclude", "-DVERSION_US=1"))
        self.assertNotIn("unbake", rendered["Makefile"].replace(str(self.root), "PROJECT"))
        self.assertNotIn("toolkit", rendered["Makefile"])
        self.assertIn("BUILD ?= build/$(VERSION)", rendered["Makefile"])

    def test_standalone_cold_warm_and_header_dependency(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        generation = self.root / "build/us.1"
        command = ["make", "-C", str(self.root), "-j2", "VERSION=us", "BUILD=" + str(generation)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(str(generation / "game.us.z64") + ": OK", first.stdout)
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(calls.count("splat"), 1)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), calls)
        (self.root / "include/value.h").write_text("#define VALUE 2\n")
        third = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        changed = (self.root / "calls").read_text().splitlines()
        self.assertEqual(changed.count("splat"), 1)
        self.assertEqual(changed.count("as"), 1)
        self.assertEqual(changed.count("cc"), 2)
        self.assertEqual(changed.count("ld"), 1)

    def test_generation_copy_has_no_old_generation_object_paths(self) -> None:
        module = helper("extract")
        staging = self.root / "scratch"
        script = f"SECTIONS {{ {staging}/asm/nonmatchings/first.s.o(.text); {staging}/assets/tail.bin.o(.data); }}"
        rewritten, graph = module.inventory(script, staging, Path("asm/us"), Path("src"), "sn64")
        self.assertIn("obj/asm/nonmatchings/first.o(.text)", rewritten)
        self.assertNotIn(str(self.root), rewritten)
        self.assertIn("$(BUILD)/obj/asm/nonmatchings/first.built: asm/us/nonmatchings/first.s", graph)

    def test_external_local_address_references_get_exact_link_symbols(self) -> None:
        module = helper("extract")
        (self.root / "first.s").write_text(
            ".L80001000:\n lui $at, %hi(.L80002000)\n lw $v0, %lo(.L80002000)($at)\n beq $v0, $zero, .L80001000\n"
        )
        self.assertEqual(module.external_labels(self.root), ".L80002000 = 0x80002000;\n")

    def test_external_branches_count_zero_operand_instructions_and_words(self) -> None:
        module = helper("resolve_external_branches")
        text = (
            ".globl renamed\n.ent renamed\nrenamed:\n nop\n eret\n .word 0, 1\n beq "
            "$4, $5, target\n jr $31\n.end renamed\n"
        )
        result = module.resolve(text, "renamed", {"renamed": 0x80001000, "target": 0x80001040}, {"renamed"})
        self.assertIn(".word 0x1085000B", result)
        self.assertIn(".word 0x03E00008", result)

    def test_numeric_branch_target_and_trap(self) -> None:
        module = helper("resolve_external_branches")
        result = module.resolve(
            "entry:\n bc1f . + 4 + (-0x2 << 2)\n teq $4, $5, 3\n", "entry", {"entry": 0x80001000}, {"entry"}
        )
        self.assertIn("0x4500FFFE", result)
        self.assertIn("0x008500F4", result)
        with self.assertRaisesRegex(ValueError, "missing"):
            module.resolve("entry:\n beq $4, $5, absent\n", "entry", {"entry": 0x80001000}, {"entry"})

    def test_external_jump_and_hilo_hazards_are_exact_instructions(self) -> None:
        module = helper("resolve_external_branches")
        text = "entry:\n j .L80002000\n nop\n mfhi $6\n div $16, $7\n"
        result = module.resolve(text, "entry", {"entry": 0x80001000}, {"entry"})
        self.assertIn("0x08000800", result)
        self.assertIn("0x00003010", result)
        self.assertIn("0x0207001A", result)

    def test_unit_flag_overrides_share_the_compiler_recipe(self) -> None:
        project, _ = fixture(self.root)
        with (self.root / "config.toml").open("a") as stream:
            stream.write('\n[build.unit_cflags]\nmiddle = ["-O1"]\n')
        self.assertEqual(makefile.flags(project, "us", "src/middle.c")[-1], "-O1")
        self.assertEqual(makefile.flags(project, "us", project.src / "middle.c")[-1], "-O1")
        self.assertEqual(makefile.description(project)["unit_cflags"]["middle"], ("-O1",))

    def test_real_splat_and_binutils_extract_link_and_compare(self) -> None:
        project, _ = fixture(self.root)
        header = bytearray(64)
        struct.pack_into(">4I", header, 0, 0x80371240, 15, 0x80001000, 0)
        header[32:52] = b"BUILD FIXTURE       "
        rom = bytes(header) + b"".join(struct.pack(">3I", 0x24020000 | value, 0x03E00008, 0) for value in (1, 2, 3))
        version = dataclasses.replace(project.version("us"), baserom_sha1=hashlib.sha1(rom).hexdigest())
        version.baserom.write_bytes(rom)
        version.split.write_text(
            "name: Fixture\noptions:\n  basename: game\n  target_path: ../../base"
            "rom.us.z64\n  base_path: ../..\n  platform: n64\n  compiler: IDO\n  f"
            "ind_file_boundaries: false\nsegments:\n  - [0x0, header, header]\n  "
            "- name: main\n    type: code\n    start: 0x40\n    vram: 0x80001000\n"
            "    subalign: 4\n    align: 4\n    subsegments:\n      - [0x40, asm,"
            " alpha]\n      - [0x4C, asm, beta]\n      - [0x58, asm, gamma]\n  - "
            "[0x64]\n"
        )
        version.symbols.write_text("alpha = 0x80001000;\nbeta = 0x8000100C;\ngamma = 0x80001018;\n")
        project = dataclasses.replace(project, version_map={"us": version})
        facts = {
            "as": tool("mips-linux-gnu-as"),
            "ld": tool("mips-linux-gnu-ld"),
            "objcopy": tool("mips-linux-gnu-objcopy"),
            "splat": str(test_policy().splat),
            "asflags": ["-march=vr4300", "-mabi=32", "-EB", "--no-pad-sections"],
        }
        (self.root / "config.toml").write_text(
            "[build]\n" + "".join(k + " = " + json.dumps(v) + "\n" for k, v in facts.items())
        )
        write_rendered(project)
        environment = dict(os.environ)
        environment.pop("PYTHONNOUSERSITE", None)
        result = subprocess.run(["make", "-C", str(self.root), "-j2"], capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        built = (self.root / "build/us/game.us.z64").read_bytes()
        self.assertEqual(built, rom)


class HostExecutableTests(unittest.TestCase):
    def test_policy_references_resolve_through_the_operator_policy(self) -> None:
        policy = SimpleNamespace(cpp=Path("/usr/bin/cpp"))
        self.assertEqual(makefile.host_executable(policy, "policy:cpp", "cpp"), "/usr/bin/cpp")  # type: ignore[arg-type]
        self.assertEqual(makefile.host_executable(policy, "tools/cpp", "cpp"), "tools/cpp")  # type: ignore[arg-type]
        with self.assertRaisesRegex(config.Held, "policy.mips_cpp: missing executable for build.cpp"):
            makefile.host_executable(policy, "policy:mips_cpp", "cpp")  # type: ignore[arg-type]
        with self.assertRaisesRegex(config.Held, "build.cpp: missing value"):
            makefile.host_executable(policy, "", "cpp")  # type: ignore[arg-type]
