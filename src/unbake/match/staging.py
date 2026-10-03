from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from unbake.layout import split, split_apply
from unbake.match.common import (
    held,
    read,
    relative,
    sha,
)
from unbake.project import build, makefile
from unbake.project.config import Project
from unbake.project_tools.compile_identity import driver_names, selected_pins
from unbake.project_tools.host import resolve_tool

# Retained trials and local environments are outputs, not cartridge build inputs.
_OUTPUTS = frozenset({".git", "artifacts", ".unbake", ".splat", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


def project_input(project: Project, path: Path) -> bool:
    """Classify a project-relative path for both staging and publication checks."""
    return _classifier(project)(path.parts)


def _classifier(project: Project) -> Callable[[tuple[str, ...]], bool]:
    """Return a parts predicate with the project's output roots resolved once."""
    outputs = tuple(root.relative_to(project.root).parts for root in (project.build, project.work, project.drafts))

    def accept(parts: tuple[str, ...]) -> bool:
        return (
            bool(parts)
            and parts[0] not in _OUTPUTS
            and not any(parts[: len(root)] == root for root in outputs)
            and not any(part in {"__pycache__", ".venv", "venv"} for part in parts)
            and parts[-1] != "clone-policy.toml"
            and not parts[-1].endswith((".pyc", ".pyo"))
        )

    return accept


def compare_failure(project: Project, version: str, result: build.BuildResult) -> str:
    cartridge = project.version(version)
    rom = result.generation / f"{project.name}.{version}.z64"
    if not rom.is_file():
        return f"VERSION {version}: produced ROM missing; {read(result.log).decode(errors='replace')[-2000:]}"
    expected, produced = read(cartridge.baserom), read(rom)
    if expected == produced:
        return f"VERSION {version}: ROM bytes agree; {read(result.log).decode(errors='replace')[-2000:]}"
    offset = next(
        (i for i, (left, right) in enumerate(zip(expected, produced, strict=False)) if left != right),
        min(len(expected), len(produced)),
    )
    _, lines, segments = split.layout(cartridge.split)
    owner = next(
        (row.path for segment in segments for row in segment.rows if row.start <= offset < split.end(row)),
        "outside split units",
    )
    if owner == "outside split units":
        # Top-level header and binary units have no code subsegment rows.
        boundaries = sorted(
            int(match[1], 0) for line in lines if (match := re.match(rf"\s*-\s*\[\s*({split.NUMBER})", line))
        )
        for line in lines:
            row = split.ROW.fullmatch(line)
            if row:
                start = int(row["start"], 0)
                stop = next((boundary for boundary in boundaries if boundary > start), len(expected))
                if start <= offset < stop:
                    owner = split.plain(row["path"])
                    break
    return (
        f"VERSION {version}: first differing ROM offset 0x{offset:X}; "
        f"expected {expected[offset : offset + 16].hex() or '<EOF>'}, "
        f"produced {produced[offset : offset + 16].hex() or '<EOF>'}; unit {owner}; "
        f"sizes {len(expected)}/{len(produced)}"
    )


def copy_tree(
    project: Project, source: Path, destination: Path, *, skip: tuple[str, ...] = (), assembly: bool = True
) -> None:
    """Copy project inputs; skip names top-level inputs the build never reads."""

    def ignore(directory: str, names: list[str]) -> list[str]:
        parent = Path(directory).relative_to(source)
        return [
            name
            for name in names
            if not project_input(project, parent / name)
            or (parent == Path(".") and name in skip)
            or (not assembly and parent / name == project.asm.relative_to(project.root))
        ]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def fingerprint(project: Project, root: Path) -> dict[str, str]:
    """Identify every project input by file identity, size and modification time.

    Writers replace files atomically or rewrite them; either changes the signature.
    """
    result = {}
    accept = _classifier(project)
    base = len(str(root).rstrip(os.sep)) + 1
    for directory, names, files in os.walk(root, followlinks=True):
        parent = tuple(Path(directory[base:]).parts) if len(directory) >= base else ()
        names[:] = [name for name in names if accept((*parent, name))]
        for name in files:
            if accept((*parent, name)):
                path = os.path.join(directory, name)
                try:
                    stat = os.stat(path)
                except OSError as error:
                    held(f"{path}: {error}")
                result[path[base:]] = f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
    return result


def generation(project: Project, version: str, current: Path, holds: ExitStack, *, retained: bool = False) -> Path:
    parent = project.build
    number = 1
    prefix = version + "."
    for path in parent.iterdir():
        suffix = path.name.removeprefix(prefix)
        if path.name.startswith(prefix) and suffix.isdigit():
            number = max(number, int(suffix) + 1)
    with build.lock(project):
        while True:
            generation = parent / f"{version}.{number}"
            try:
                generation.mkdir()
                holds.enter_context(build.pin(generation))
                break
            except FileExistsError:
                number += 1
    try:
        if retained:
            for path in current.iterdir():
                if path.name in {".inuse", "report", "obj"} or path.suffix in {".elf", ".map", ".z64"}:
                    continue
                if path.is_dir():
                    shutil.copytree(path, generation / path.name, symlinks=True)
                else:
                    shutil.copy2(path, generation / path.name)
            objects = generation / "obj"
            objects.mkdir()
            for path in (current / "obj").iterdir():
                target = objects / path.name
                if path.name in {"asm", "assets"}:
                    target.symlink_to(path.resolve(), target_is_directory=True)
                elif path.is_dir():
                    shutil.copytree(path, target, symlinks=True)
                else:
                    shutil.copy2(path, target)
            return generation
        result = subprocess.run(
            ["cp", "-a", "--reflink=auto", *(str(p) for p in current.iterdir() if p.name != ".inuse"), str(generation)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            held(f"VERSION {version}: cp {current} to {generation}: {result.stderr.strip()}")
        return generation
    except BaseException:
        holds.close()
        build.discard_generation(generation)
        raise


def chunk_stale_sources(generation: Path, tools: Path, symbols: Path) -> None:
    """Let the ordinary Make cold-chunk rule refresh outdated C/assembly receipts.

    Keep objects and dependency files: the compiler still verifies their content
    keys, while Make avoids starting one interpreter per stale source receipt.
    """
    recipe = tools / "build.json"
    if not recipe.is_file():
        return
    data = json.loads(recipe.read_text())
    version = generation.name.rsplit(".", 1)[0]
    groups: dict[str, dict[str, str]] = {}
    manifest = tools / "compiler.sha256"
    if manifest.is_file():
        for row in manifest.read_text().splitlines():
            fields = row.split(maxsplit=1)
            if len(fields) == 2 and not row.startswith("#"):
                name = fields[1].lstrip("*")
                groups.setdefault(str(Path(name).parent), {})[name] = fields[0]
    root = tools.parent
    executables: dict[str, Path] = {}

    def executable(value: str) -> Path:
        if value not in executables:
            executables[value] = (
                root / value if "/" in value and not value.startswith("policy:") else Path(resolve_tool(value))
            )
        return executables[value]

    independent_objects(generation)
    for kind in ("src", "asm"):
        base = generation / "obj" / kind
        for receipt in base.rglob("*.built"):
            if receipt.is_symlink():
                continue
            unit = receipt.relative_to(base).with_suffix("").as_posix()
            ident = (
                data["units"].get(Path(unit).stem, data["default_compiler"])
                if kind == "src"
                else data["assembly_compiler"]
            )
            compiler = data["compilers"][ident] if ident else None
            sn64 = compiler is not None and compiler["kind"] == "sn64"
            inputs = [
                tools / "compile/drivers" / (name + ".sha256")
                for name in driver_names("cc" if kind == "src" else "as", sn64)
            ]
            inputs.append(tools / "compile" / version / ((ident if kind == "src" else "assembly") + ".json"))
            if kind == "src" and compiler:
                cc = Path(compiler["cc"])
                inputs.extend(
                    root / name
                    for name in {str(cc), *selected_pins(groups, cc, tools.relative_to(root), compiler["kind"])}
                )
                inputs.append(tools / "compile/units" / (unit + ".json"))
            if sn64:
                assert compiler is not None
                inputs.extend((tools / "compile/drivers/abumasn64.sha256", executable(compiler["as"])))
            elif kind == "asm" and data.get("as"):
                inputs.append(executable(data["as"]))
            if kind == "asm" and sn64:
                inputs.append(symbols)
            if any(path.is_file() and path.stat().st_mtime_ns > receipt.stat().st_mtime_ns for path in inputs):
                receipt.unlink()


def project_at(project: Project, tree: Path) -> Project:
    """Relocate mutable build inputs to an isolated tree."""

    def relocated(path: Path) -> Path:
        return tree / path.relative_to(project.root) if path.is_relative_to(project.root) else path

    return replace(
        project,
        root=tree,
        tools=relocated(project.tools),
        asm=relocated(project.asm),
        roms=relocated(project.roms),
        build=relocated(project.build),
        work=relocated(project.work),
        drafts=relocated(project.drafts),
        compilers={
            ident: replace(
                compiler, cc=relocated(compiler.cc), as_=relocated(compiler.as_), sha256=relocated(compiler.sha256)
            )
            for ident, compiler in project.compilers.items()
        },
        src=tree / relative(project, project.src),
        include=tuple(tree / relative(project, path) for path in project.include),
        version_map={
            v: replace(
                project.version(v),
                baserom=relocated(project.version(v).baserom),
                split=tree / relative(project, project.version(v).split),
                symbols=tree / relative(project, project.version(v).symbols),
            )
            for v in project.versions
        },
    )


def helper_edits(project: Project) -> list[split.Edit]:
    """Stage current generated drivers with verified checksum replacements."""
    edits = []
    rendered = makefile.render(project)
    for relative_path, content in rendered.items():
        path = project.root / relative_path
        if (
            path.suffix == ".py"
            or path.is_relative_to(project.tools / "compile")
            or path == project.root / "Makefile"
            or path.name in {"link.json", "extract.json"}
        ) and (before := path.read_text() if path.exists() else "") != content:
            edits.append(split.Edit(path, before, content, project.versions))
    if not edits:
        return []
    checksum = project.tools / "compiler.sha256"
    before_checksum = checksum.read_text()
    lines = before_checksum.splitlines(keepends=True)
    for edit in edits:
        if edit.path == project.root / "Makefile":
            continue
        name = edit.path.relative_to(project.root).as_posix()
        entries = [i for i, line in enumerate(lines) if line.strip().split(maxsplit=1)[1:] == [name]]
        if edit.path.exists():
            if len(entries) != 1 or lines[entries[0]].split()[0] != sha(edit.before.encode()):
                held(f"submit.helper_checksum: {name}: expected one verified generated helper entry")
            lines[entries[0]] = f"{sha(edit.after.encode())}  {name}\n"
        elif entries:
            held(f"submit.helper_checksum: {name}: declared generated helper is missing")
        else:
            lines.append(f"{sha(edit.after.encode())}  {name}\n")
    return [*edits, split.Edit(checksum, before_checksum, "".join(lines), project.versions)]


def write_staged(project: Project, edits: Iterable[split.Edit]) -> None:
    """Apply the complete publication edits only inside its private proof tree."""
    edits = split_apply.coalesce(edits)
    configured = {
        project.root / "config.toml",
        project.root / "Makefile",
        project.tools / "link.json",
        project.tools / "extract.json",
        project.root / "unbake-exclusions.json",
        project.tools / "build.json",
        project.tools / "compiler.sha256",
        *(
            p
            for version in project.versions
            for p in (project.version(version).split, project.version(version).symbols)
        ),
        *(project.tools / name.name for name in makefile.TEMPLATES.glob("*.py")),
        project.tools / "cache.py",
    }
    for edit in edits:
        relative(project, edit.path)
        if (
            edit.path not in configured
            and not edit.path.is_relative_to(project.tools / "compile")
            and not any(edit.path.is_relative_to(root) for root in (project.src, *project.include))
        ):
            held(f"{edit.path}: outside publication inputs")
        if (edit.path.read_text() if edit.path.exists() else "") != edit.before:
            prerequisite = (
                "headers.declaration: " if any(edit.path.is_relative_to(root) for root in project.include) else ""
            )
            held(f"{prerequisite}{edit.path}: changed since publication preview")
        for version in edit.versions:
            project.version(version)
    written = []
    try:
        for edit in edits:
            exists = edit.path.exists()
            split_apply.write(edit.path, edit.after)
            written.append((edit, exists))
    except BaseException:
        for edit, exists in reversed(written):
            if exists:
                split_apply.write(edit.path, edit.before)
            else:
                edit.path.unlink(missing_ok=True)
        raise


def copy_assembly(project: Project, staged: Project) -> None:
    """Hydrate assembly only when a changed boundary requires extraction."""
    if not staged.asm.exists():
        if project.asm.is_dir():
            shutil.copytree(project.asm, staged.asm, symlinks=True)
        else:
            staged.asm.mkdir(parents=True)


def independent_objects(generation: Path) -> None:
    """Detach retained assembly before a fallback Make can write through its directory."""
    for name in ("asm", "assets"):
        path = generation / "obj" / name
        if path.is_symlink():
            source = path.resolve()
            path.unlink()
            shutil.copytree(source, path, symlinks=True)


def publication_stamps(project: Project, generations: dict[str, Path]) -> None:
    """Mark the successful proof's graph and objects current after input writes.

    Called under the publication lock: all project inputs still equal the proved
    staged tree. This changes receipts only, leaving object and ROM bytes intact.
    Future input edits remain newer and follow ordinary Make dependency rules.
    """
    for generation in generations.values():
        # Retained assembly directories belong to an older generation. Receipt
        # updates must never write through those shared directory symlinks.
        independent_objects(generation)
        for receipt in (generation / "obj").rglob("*.built"):
            if not receipt.is_symlink():
                receipt.touch()
        for name in (".split.mk", ".split"):
            path = generation / name
            if path.is_file():
                path.touch()
