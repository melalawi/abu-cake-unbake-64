"""Shared standalone project fixtures and helper loading."""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.support import tool, write_policy
from unbake.project import config, makefile

WORK = Path(tempfile.gettempdir())


def helper(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, makefile.TEMPLATES / (name + ".py"))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load project helper {name}")
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
