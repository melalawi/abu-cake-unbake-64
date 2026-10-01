"""Create a repository shell without ROM, policy, compiler or build work."""

import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from unbake.project.config import SCHEMA_VERSION, Held


def run(target: Path) -> list[str]:
    target = Path(target).expanduser().absolute()
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise Held("init", f"init.target: {target}: symlink")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise Held("init", f"init.target: {target}: expected empty directory")
    if target.name in ("", ".", "..") or any(ord(character) < 32 for character in target.name):
        raise Held("init", f"init.target: {target}: invalid filesystem name")
    git = shutil.which("git")
    if git is None:
        raise Held("init", "git: missing executable")
    template = Path(__file__).parents[1] / "project_tools" / "CONTRIBUTING.pending.md"
    try:
        contributing = template.read_text()
    except OSError as error:
        raise Held("init", f"init.docs: {template}: {error}") from error
    existed = target.exists()
    target.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run([git, "init", "-b", "main"], cwd=target, capture_output=True, text=True)
        if result.returncode:
            raise Held("init", f"git: {result.stderr.strip()}")
        quote = json.dumps
        (target / "config.toml").write_text(
            f"schema = {SCHEMA_VERSION}\n\n[project]\n"
            f'id = {quote(str(uuid4()))}\nstate = "awaiting-roms"\n\n'
            f"[workspace]\nid = {quote(str(uuid4()))}\n\n"
            '[paths]\nroms = "roms"\nbuild = "build"\nwork = "build/work"\n'
            'drafts = "build/drafts"\nsrc = "src"\ninclude = ["include"]\nasm = "asm"\ntools = "tools"\n'
        )
        (target / ".gitignore").write_text("/roms/\n/build/\n/asm/\n/.splat/\n__pycache__/\n*.py[cod]\n")
        (target / "roms").mkdir()
        (target / "README.md").write_text(
            f"# {target.name}\n\nPut your ROMs in `roms/`. Run `unbake setup` in this repo.\n"
        )
        (target / "CONTRIBUTING.md").write_text(contributing)
    except BaseException:
        shutil.rmtree(target)
        if existed:
            target.mkdir()
        raise
    return [f"{target}: repository created", f"ROM folder: {target / 'roms'}"]
