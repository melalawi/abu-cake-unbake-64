from __future__ import annotations

import hashlib
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
from unbake.project_tools.compile_identity import driver_names, driver_stamp_name

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
    project: Project,
    source: Path,
    destination: Path,
    *,
    skip: tuple[str, ...] = (),
    assembly: bool = True,
    linked: bool = False,
) -> None:
    """Copy project inputs; linked staging requires editors that replace paths.

    Other callers get independent writable copies. Output directories are pruned
    before traversal, including the entire build and retained scratch trees.
    """

    accept = _classifier(project)

    def ignore(directory: str, names: list[str]) -> list[str]:
        parent = Path(directory).relative_to(source)
        return [
            name
            for name in names
            if not accept((parent / name).parts)
            or (parent == Path(".") and name in skip)
            or (not assembly and parent / name == project.asm.relative_to(project.root))
        ]

    shutil.copytree(
        source, destination, ignore=ignore, symlinks=True, copy_function=os.link if linked else shutil.copy2
    )
    from unbake.layout import index

    lookup = index.path(project)
    if lookup.is_file():
        target = destination / lookup.relative_to(project.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lookup, target)


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


def generation(
    project: Project, version: str, current: Path, holds: ExitStack, *, retained: bool = False, borrowed: bool = False
) -> Path:
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
            retain(current, generation, borrowed=borrowed)
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


def object_paths(generation: Path) -> tuple[Path, ...]:
    """Read the extraction's object inventory, without visiting the build tree."""
    graph = generation / ".split.mk"
    if not graph.is_file():
        return ()
    names = re.findall(r"\$\(BUILD\)/(obj/[^\s:]+\.o)(?=\s|$)", graph.read_text())
    paths = {Path(name) for name in names}
    if any(path.is_absolute() or ".." in path.parts for path in paths):
        held(f"submit.objects: {graph}: object outside generation")
    return tuple(sorted(paths))


def _retain_object(source: Path, target: Path) -> None:
    """Link immutable object bytes; keep timestamp receipts on private inodes.

    Compiler and placement writers replace output paths atomically. Hard links
    keep resolve() local, unlike a symlink that could redirect a compiler write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".o", ".d", ".inputs.json", ".built"):
        old, new = source.with_suffix(suffix), target.with_suffix(suffix)
        if old.is_file():
            if suffix == ".built":
                shutil.copy2(old, new)
            else:
                os.link(old, new, follow_symlinks=True)


def retain(current: Path, generation: Path, *, borrowed: bool = False) -> None:
    """Retain known link inputs, without copying unrelated build artifacts."""
    for path in current.iterdir():
        if path.name in {".inuse", "report", "obj", "retained-layout.json"} or path.suffix in {".elf", ".map", ".z64"}:
            continue
        if path.is_file():
            shutil.copy2(path, generation / path.name)
    if borrowed:
        (generation / "obj").symlink_to(current / "obj", target_is_directory=True)
        return
    for kind in ("src", "asm", "assets"):
        (generation / "obj" / kind).mkdir(parents=True, exist_ok=True)
    for relative_path in object_paths(current):
        _retain_object(current / relative_path, generation / relative_path)


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
    independent_objects(generation)
    indexed = object_paths(generation) if (generation / ".split.mk").is_file() else None
    for kind in ("src", "asm"):
        base = generation / "obj" / kind
        receipts = (
            ((generation / obj).with_suffix(".built") for obj in indexed if obj.parts[1] == kind)
            if indexed is not None
            else base.rglob("*.built")
        )
        for receipt in receipts:
            if not receipt.is_file():
                continue
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
                tools / "compile/drivers" / driver_stamp_name(name, "cc" if kind == "src" else "as", sn64)
                for name in driver_names("cc" if kind == "src" else "as", sn64)
            ]
            inputs.append(tools / "compile" / version / ((ident if kind == "src" else "assembly") + ".json"))
            if kind == "src" and compiler:
                inputs.append(tools / "compile/binaries" / (ident + ".sha256"))
                inputs.append(tools / "compile/units" / (unit + ".json"))
            if sn64:
                assert compiler is not None
                inputs.extend(
                    (
                        tools / "compile/drivers/abumasn64.sha256",
                        tools / "compile/binaries" / (hashlib.sha256(compiler["as"].encode()).hexdigest() + ".sha256"),
                    )
                )
                if data.get("cpp"):
                    inputs.append(
                        tools / "compile/binaries" / (hashlib.sha256(data["cpp"].encode()).hexdigest() + ".sha256")
                    )
            elif kind == "asm" and data.get("as"):
                inputs.append(
                    tools / "compile/binaries" / (hashlib.sha256(data["as"].encode()).hexdigest() + ".sha256")
                )
            if kind == "asm" and sn64:
                inputs.append(generation / "asm-symbols" / (unit + ".txt"))
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
    objects = generation / "obj"
    if objects.is_symlink():
        source = objects.resolve()
        objects.unlink()
        objects.mkdir()
        for obj in object_paths(source.parent):
            _retain_object(source / obj.relative_to("obj"), generation / obj)
    for name in ("asm", "assets"):
        path = generation / "obj" / name
        if path.is_symlink():
            source = path.resolve()
            path.unlink()
            path.mkdir()
            for obj in object_paths(generation):
                if obj.parts[1] == name:
                    _retain_object(source / obj.relative_to(Path("obj") / name), generation / obj)


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
        for obj in object_paths(generation):
            if obj.parts[1] not in {"src", "asm"}:
                continue
            receipt = (generation / obj).with_suffix(".built")
            if receipt.is_file() and not receipt.is_symlink():
                receipt.touch()
        for name in (".split.mk", ".split"):
            path = generation / name
            if path.is_file():
                path.touch()
