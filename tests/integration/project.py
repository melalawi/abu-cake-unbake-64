"""A tiny fixture project with fake tool scripts, a real git repo.

The fake tools stand in for the cross toolchain: cpp passes text through, the compiler emits the three
instruction words of `return N;` in explicit MIPS ELF32 tables. n64link copies the relocatable
fixture object; ld and objcopy extract its .text bytes or produce a linked ELF. Make and git are the real ones.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture import make_rom
from tests.kit import host_values
from unbake.buildfiles import N64LINK_RELEASE

FIXTURE = Path(__file__).resolve().parents[1] / "fixture"
SRC = Path(__file__).resolve().parents[2] / "src"
REPO = Path(__file__).resolve().parents[2]
PYTHON_TOOL = f"#!{sys.executable}\nimport sys\nsys.path[:0] = {[str(SRC), str(REPO)]!r}\n"

COPY = """#!/bin/sh
# copy: the last two plain arguments are input and output
set -- $(for a in "$@"; do case "$a" in -*|asn64) ;; *) printf '%s ' "$a" ;; esac; done)
for a in "$@"; do prev="$last"; last="$a"; done
cp "$prev" "$last"
"""
N64LINK = (
    PYTHON_TOOL
    + f"RELEASE = {N64LINK_RELEASE!r}\n"
    + """import argparse, shutil, sys
from unbake.objects.elf import Object
if sys.argv[1:] == ["--version"]:
    print(RELEASE, end="")
    raise SystemExit(0)
parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["place"])
parser.add_argument("input")
parser.add_argument("-o", required=True)
parser.add_argument("--rom", required=True)
parser.add_argument("--text", required=True)
parser.add_argument("--map", action="append", default=[])
parser.add_argument("--symbols", required=True)
parser.add_argument("--trim", action="store_true")
parser.add_argument("--score", action="store_true")
args = parser.parse_args()
Object(args.input)
shutil.copyfile(args.input, args.o)
"""
)
CC = (
    PYTHON_TOOL
    + r"""import os, re, shlex, struct, subprocess, sys
from pathlib import Path
from tests.elf_fixture import write_object
args = sys.argv[1:]
source = args[-1]
if "-show" in args:
    print(f"/usr/lib/cfe -D__sgi -I/usr/include {source} -E -D_LANGUAGE_C -std", file=sys.stderr)
    raise SystemExit(0)
if "-E" in args or "-M" in args:
    flags = [a for a in args if a.startswith(("-I", "-D", "-U")) or a in ("-E", "-M", "-P")]
    if "-M" in args:
        output = subprocess.check_output(["/usr/bin/cpp", *flags, source], text=True)
        target, _, names = output.replace("\\\n", " ").partition(":")
        for name in shlex.split(names):
            print(f"{target}: {name}")
        raise SystemExit(0)
    os.execv("/usr/bin/cpp", ["cpp", *flags, source])
out_at = args.index("-o")
out = args[out_at + 1]
source = next(a for i, a in enumerate(args) if not a.startswith("-") and i != out_at + 1)
text = Path(source).read_text()
function = re.search(r"(\w+)\s*\([^)]*\)\s*\{\s*return\s+(\d+);", text)
if function is None:
    raise SystemExit("fixture compiler: expected an integer-return function")
name, value = function.groups()
code = struct.pack(">3I", 0x24020000 | int(value), 0x03E00008, 0)
write_object(Path(out), {".text": code}, [(name, ".text", 0, len(code))])
"""
)
LD = (
    PYTHON_TOOL
    + """import sys
from pathlib import Path
from tests.elf_fixture import linked_fixture
from unbake.objects.elf import Object
args = iter(sys.argv[1:])
files, binary, address = [], False, 0
for arg in args:
    if arg == "-o":
        out = Path(next(args))
    elif arg == "--oformat":
        binary = next(args) == "binary"
    elif arg in ("-T", "-Map", "--defsym"):
        next(args)
    elif arg.startswith("--section-start=.text="):
        address = int(arg.split("=", 2)[2], 0)
    elif not arg.startswith("-"):
        files.append(Path(arg))
if binary:
    out.write_bytes(b"".join(Object(p).content(Object(p).section(".text")) for p in files))
else:
    linked_fixture(out, files, {".text": address})
"""
)
OBJCOPY = (
    PYTHON_TOOL
    + """import sys
from pathlib import Path
from unbake.objects.elf import Object
args = iter(sys.argv[1:])
files, section = [], ".text"
for arg in args:
    if arg == "-j":
        section = next(args)
    elif arg in ("-I", "-O", "--rename-section", "--pad-to", "--gap-fill"):
        next(args)
    elif not arg.startswith("-"):
        files.append(Path(arg))
obj = Object(files[0])
index = obj.section(section)
if index is None:
    raise SystemExit(f"fixture objcopy: missing {section}")
files[1].write_bytes(obj.content(index))
"""
)
OBJDIFF = "#!/bin/sh\nexit 0\n"
SPLAT = (
    PYTHON_TOOL
    + """import csv, json, struct, sys
from pathlib import Path
from tests.project_fixture import assembly
from unbake import config
from unbake.layout import split
options = {}
for line in Path(sys.argv[-1]).read_text().splitlines()[1:]:
    name, _, value = line.strip().partition(": ")
    options[name] = json.loads(value)
project = config.load(Path.cwd())
version = next(v for v in project.versions if str(project.version(v).baserom.resolve()) == options["target_path"])
staging = Path(options["base_path"])
(staging / ".splat").mkdir()
with (staging / ".splat/splat_symbols.csv").open("w", newline="") as stream:
    writer = csv.writer(stream)
    writer.writerow(["name", "vram_start"])
    for row in split.functions(project, version):
        writer.writerow([row.name, hex(row.address)])
        path = staging / "asm" / (row.path + ".s")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = split.words(project, row)
        path.write_text(assembly(row.name, list(struct.unpack(">" + "I" * (len(data) // 4), data))))
"""
)
# What the generated Makefile runs besides the fixture tools; a [tools].path dir must not expose Python.
HOST_COMMANDS = [
    "sh",
    "cat",
    "cmp",
    "cp",
    "cut",
    "dd",
    "dirname",
    "env",
    "find",
    "grep",
    "head",
    "ls",
    "mkdir",
    "mv",
    "printf",
    "rm",
    "sed",
    "sha1sum",
    "sha256sum",
    "sort",
    "tail",
    "tr",
]


def run(command: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True, **kwargs)


class FixtureCase(unittest.TestCase):
    """self.root is a committed project; self.unbake(...) runs the CLI."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.base, self.root = base, base / "project"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("__pycache__", "__init__.py", "make_rom.py"))
        make_rom.write_roms(self.root)
        # These input C files are draft proposals, with assembly still owning
        # every build row. Keep them outside the retained-source inventory.
        for source in (self.root / "src").glob("*.c"):
            proposal = self.root / "build" / "work" / source.stem / source.name
            proposal.parent.mkdir(parents=True, exist_ok=True)
            source.rename(proposal)
        self.install_tools()
        self.write_host()
        ignore = self.root / ".gitignore"
        ignore.write_text(
            (ignore.read_text() if ignore.exists() else "")
            + "\n/roms/\n/build/\n/asm/\n/.splat/\n/.unbake/\n/tools/ido-7.1/\n/tools/.downloads/\n/.attempts.lock\n"
        )
        self.init_git()

    def install_tools(self) -> None:
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        scripts = {
            "cpp": '#!/bin/sh\nexec /usr/bin/cpp "$@"\n',
            "mips_as": COPY,
            "n64link": N64LINK,
            "mips_ld": LD,
            "mips_objcopy": OBJCOPY,
            "splat": SPLAT,
            "m2c": OBJDIFF,
            "mips_objdump": OBJDIFF,
            "mips_readelf": OBJDIFF,
        }
        for name, text in scripts.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        cc = self.root / "tools" / "ido-7.1" / "cc"
        cc.parent.mkdir(parents=True, exist_ok=True)
        cc.write_text(CC)
        cc.chmod(0o755)
        (self.root / "tools" / "compilers.sha256").write_text(
            f"{hashlib.sha256(CC.encode()).hexdigest()}  tools/ido-7.1/cc\n"
        )
        self.bin = bin_dir

    def write_host(self) -> None:
        # host_values writes stub tools into DIR/bin; keep them out of self.bin, which holds the fixture tools.
        (self.base / "kit").mkdir()
        values = host_values(self.base / "kit")
        # These isolated public fixtures own their worker budget and use no live broker.
        values["resources"]["domain"] = "standalone"
        tools = values["tools"]
        for key in (
            "cpp",
            "mips_as",
            "mips_ld",
            "mips_objcopy",
            "mips_objdump",
            "mips_readelf",
            "n64link",
            "splat",
            "m2c",
        ):
            tools[key] = str(self.bin / key)
        tools["make"] = shutil.which("make") or "/usr/bin/make"
        host_bin = self.base / "hostbin"
        host_bin.mkdir()
        for name in HOST_COMMANDS:
            found = shutil.which(name)
            if found:
                (host_bin / name).symlink_to(found)
        tools["path"] = [str(self.bin), str(host_bin)]
        values["cache"]["machine_root"] = str(self.base / "cache")
        values["cache"]["max_bytes"], values["cache"]["trim_to_bytes"] = 10**8, 10**7
        values["publish"].update(branch="main")
        lines = []
        for section, table in values.items():
            lines.append(f"[{section}]")
            lines += [f"{key} = {json.dumps(value)}" for key, value in table.items()]
        self.host_file = self.base / "unbake.toml"
        self.host_file.write_text("\n".join(lines) + "\n")

    def init_git(self) -> None:
        run(["git", "init", "-b", "main"], self.root)
        for key, value in (("user.name", "Mo"), ("user.email", "mo@example.com")):
            run(["git", "config", key, value], self.root)
        run(["git", "add", "-A"], self.root)
        run(["git", "commit", "-qm", "Fixture"], self.root)

    def work(self, function: str, text: str) -> Path:
        path = self.root / "build" / "work" / function / f"{function}.c"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def unbake(self, *args: str, cwd: Path | None = None) -> tuple[int, list[dict], str]:
        env = {**os.environ, "PYTHONPATH": f"{SRC}:{REPO}", "PYTHONDONTWRITEBYTECODE": "1"}
        command = [sys.executable, "-m", "unbake", "--config", str(self.host_file), *args]
        done = subprocess.run(command, cwd=cwd or self.root, env=env, text=True, capture_output=True)
        lines = [json.loads(line) for line in done.stdout.splitlines()]
        return done.returncode, lines, done.stderr

    def subjects(self) -> list[str]:
        """Commit subjects of the project, newest first."""
        return run(["git", "log", "--format=%s"], self.root).stdout.splitlines()
