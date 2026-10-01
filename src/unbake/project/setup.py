"""Verify supplied project inputs before publishing standalone build files."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from unbake.layout import shared
from unbake.project import compiler_files, hygiene, makefile, toolchain
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.host import resolve_tool


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise Held("setup", f"{path}: {error}") from error


def _comment(line: str) -> bool:
    quote, escaped = None, False
    for character in line:
        if escaped:
            escaped = False
        elif character == "\\" and quote == '"':
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "#":
            return True
    return False


def verify_compiler(project: Project) -> None:
    for ident in project.compilers:
        toolchain.verify(project.tools / ident, toolchain.specification(ident))


def restore_roms(project: Project, source: Path) -> None:
    """Restore missing VERSION inputs from a directory using declared SHA-1 pins."""
    missing = [project.version(name) for name in project.versions if not project.version(name).baserom.is_file()]
    if not missing:
        return
    contents = compiler_files.directory_files(source, {version.baserom_sha1 for version in missing}, "sha1")
    for version in missing:
        if version.baserom_sha1 not in contents:
            raise Held(
                "setup",
                f"[version.{version.name}].baserom_sha1: {source}: missing supplied SHA-1 {version.baserom_sha1}",
            )
    for version in missing:
        target = project.root / version.baserom
        if target.is_symlink():
            raise Held("setup", f"{target}: baserom symlink target is missing")
        compiler_files.atomic_bytes(target, contents[version.baserom_sha1])


def run(project: Project, policy: Policy, *, new_rom: Path | None = None) -> list[str]:
    config_path = project.root / "config.toml"
    config_text = config_path.read_text()
    build = makefile.recipe(project)
    for field, value, name in (
        ("ld", build.ld, "mips_ld"),
        ("objcopy", build.objcopy, "mips_objcopy"),
        ("splat", build.splat, "splat"),
        ("as", build.as_, "mips_as"),
        ("cpp", build.cpp, "cpp"),
    ):
        if value is not None:
            portable = makefile.host_tool(project, value, name)
            try:
                configured = getattr(policy, name, portable) if portable.startswith("policy:") else portable
                if not str(configured).startswith("policy:") and "/" in str(configured):
                    configured = project.root / str(configured)
                resolve_tool(str(configured))
            except (AttributeError, OSError, ValueError) as error:
                raise Held("setup", f"policy.{name}: {error}") from error
            config_text = re.sub(rf"^{field}\s*=.*$", f"{field} = {json.dumps(portable)}", config_text, flags=re.M)
    files = makefile.render(project)
    files[".gitignore"] = hygiene.ignore_text(project)
    toolchain.ensure(project, policy)
    verify_compiler(project)
    receipts = []
    for name in project.versions:
        version = project.version(name)
        rom = new_rom if new_rom is not None and name == project.names_from else version.baserom
        rom = project.root / rom
        rom_content = _read(rom)
        digest = hashlib.sha1(rom_content).hexdigest()
        if digest != version.baserom_sha1:
            value = "new_rom" if new_rom is not None and name == project.names_from else "baserom_sha1"
            raise Held("setup", f"{rom}: [version.{name}].{value} expected {version.baserom_sha1}, got {digest}")
        split = project.root / version.split
        text = _read(split).decode()
        for number, line in enumerate(text.splitlines(), 1):
            if _comment(line):
                raise Held("setup", f"{split}:{number}: split YAML comment")
        _read(project.root / version.symbols)
        files[f"versions/{name}/baserom.sha1"] = f"{digest}  baserom.{name}.z64\n"
        files[f"versions/{name}/{project.name}.sha1"] = f"{digest}  build/{name}/{project.name}.{name}.z64\n"
        receipts.append(f"OK(setup): {name}: baserom and compiler verified; standalone build rendered")
    if new_rom is not None:
        # Existing facts identify this dump; headers alone cannot state a new compiler/split.
        target = project.root / project.version(project.names_from).baserom
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read(project.root / new_rom))
    shared.consolidate(project)
    if config_text != config_path.read_text():
        compiler_files.atomic_bytes(config_path, config_text.encode())
    for relative, content in files.items():
        destination = project.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.read_text() != content:
            compiler_files.atomic_bytes(destination, content.encode())
    manifest = project.tools / "compiler.sha256"
    generated = set(files)
    pins = "".join(
        line + "\n"
        for line in manifest.read_text().splitlines()
        if len(line.split(maxsplit=1)) != 2 or line.split(maxsplit=1)[1] not in generated
    )
    for relative in files:
        if relative.startswith(str(project.tools.relative_to(project.root)) + "/"):
            digest = hashlib.sha256((project.root / relative).read_bytes()).hexdigest()
            pins += f"{digest}  {relative}\n"
    if manifest.read_text() != pins:
        compiler_files.atomic_bytes(manifest, pins.encode())
    return receipts
