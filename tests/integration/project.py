"""A tiny fixture project with fake tool scripts, a real git repo and a local bare remote.

The fake tools stand in for the cross toolchain: cpp passes text through, the compiler emits the three
instruction words of `return N;`, as/n64link/objcopy copy bytes and ld (any --oformat) concatenates its inputs in
command-line order. Make and git are the real ones.
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

CPP = """#!/bin/sh
out=""; prev=""
for a in "$@"; do [ "$prev" = "-MF" ] && : > "$a"; prev="$a"; done
for a in "$@"; do last="$a"; done
case "$last" in -) cat ;; -*) cat ;; *) cat "$last" ;; esac
"""
COPY = """#!/bin/sh
# copy: the last two plain arguments are input and output
set -- $(for a in "$@"; do case "$a" in -*|asn64) ;; *) printf '%s ' "$a" ;; esac; done)
for a in "$@"; do prev="$last"; last="$a"; done
cp "$prev" "$last"
"""
N64LINK = (
    f"#!/bin/sh\nif [ \"$1\" = --version ]; then printf '%s' '{N64LINK_RELEASE}'; exit 0; fi\n" + COPY.split("\n", 1)[1]
)
CC = """#!/bin/sh
# emit `li v0, N; jr ra; nop` for the first `return N;` in the input
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) in="$1"; shift ;; esac; done
n=$(sed -n 's/.*return \\([0-9]*\\);.*/\\1/p' "$in" | head -1)
printf "\\044\\002\\000\\\\$(printf %03o "$n")\\003\\340\\000\\010\\000\\000\\000\\000" > "$out"
"""
LD = """#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in
  -o) out="$2"; shift 2 ;;
  -T|-Map|--defsym|--oformat) shift 2 ;;
  -*) shift ;;
  *) files="$files $1"; shift ;;
esac; done
cat $files > "$out"
"""
OBJCOPY = """#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in
  -I|-O|--rename-section|--pad-to|--gap-fill) shift 2 ;;
  -*) shift ;;
  *) files="$files $1"; shift ;;
esac; done
set -- $files
cp "$1" "$2"
"""
OBJDIFF = "#!/bin/sh\nexit 0\n"
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
    """self.root is a committed project; self.remote a bare repo; self.unbake(...) runs the CLI."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.base, self.root, self.remote = base, base / "project", base / "remote.git"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("__pycache__", "__init__.py", "make_rom.py"))
        make_rom.write_roms(self.root)
        self.install_tools()
        self.write_host()
        self.init_git()

    def install_tools(self) -> None:
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        scripts = {
            "cpp": CPP,
            "mips_as": COPY,
            "n64link": N64LINK,
            "mips_ld": LD,
            "mips_objcopy": OBJCOPY,
            "splat": OBJDIFF,
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
        values["cache"]["root"] = str(self.base / "cache")
        values["cache"]["max_bytes"], values["cache"]["trim_to_bytes"] = 10**8, 10**7
        values["publish"].update(remote=str(self.remote), branch="main")
        lines = []
        for section, table in values.items():
            lines.append(f"[{section}]")
            lines += [f"{key} = {json.dumps(value)}" for key, value in table.items()]
        self.host_file = self.base / "unbake.toml"
        self.host_file.write_text("\n".join(lines) + "\n")

    def init_git(self) -> None:
        run(["git", "init", "--bare", "-b", "main", str(self.remote)], self.base)
        run(["git", "init", "-b", "main"], self.root)
        for key, value in (("user.name", "Mo"), ("user.email", "mo@example.com")):
            run(["git", "config", key, value], self.root)
        run(["git", "remote", "add", "origin", str(self.remote)], self.root)
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

    def remote_subjects(self) -> list[str]:
        out = run(["git", "--git-dir", str(self.remote), "log", "--format=%s", "main"], self.base).stdout
        return out.splitlines()
