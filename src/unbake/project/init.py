"""Create a repository shell without ROM, policy, compiler or build work."""

import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from unbake.project import hygiene
from unbake.project.config import SCHEMA_VERSION, Held
from unbake.project_tools import atomic as atomic_files


def readme_text(target: Path) -> str:
    """A minimal owner document with a tool-owned Progress body."""
    template = Path(__file__).parents[1] / "project_tools" / "README.ready.md"
    return template.read_text().replace("@TITLE@", target.name)


def run(target: Path, *, layout_cap: int) -> list[str]:
    from unbake.layout.map import positive

    positive(layout_cap, "project.layout_cap")
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
        atomic_files.text(
            target / "config.toml",
            f"schema = {SCHEMA_VERSION}\n\n[project]\n"
            f'id = {quote(str(uuid4()))}\nstate = "awaiting-roms"\nlayout_cap = {layout_cap}\n\n'
            '[paths]\nroms = "roms"\nbuild = "build"\nwork = "build/work"\n'
            'drafts = "build/drafts"\nsrc = "src"\ninclude = ["include"]\nasm = "asm"\ntools = "tools"\n',
        )
        from unbake.layout.map import Map, encoded

        atomic_files.write(target / "layout.toml", encoded(Map(layout_cap, ())))
        atomic_files.text(target / ".gitignore", hygiene.base_ignore_text(target))
        from unbake.project.config import checkout_identity

        checkout_identity(target)
        (target / "roms").mkdir()
        atomic_files.text(target / "README.md", readme_text(target))
        atomic_files.text(target / "CONTRIBUTING.md", contributing)
    except BaseException:
        shutil.rmtree(target)
        if existed:
            target.mkdir()
        raise
    return [f"{target}: repository created", f"ROM folder: {target / 'roms'}"]
