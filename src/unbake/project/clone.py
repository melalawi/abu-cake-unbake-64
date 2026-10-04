"""Materialize an isolated project with its current warm build generations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

from unbake.project import build, compiler_files, config, hygiene, makefile, setup, toolchain
from unbake.project.config import Held, Policy, Project
from unbake.project_tools import atomic as atomic_files


def project_relative(project: Project, path: Path) -> Path:
    try:
        relative = path.relative_to(project.root)
    except ValueError as error:
        raise Held("clone", f"{path}: required project-local input") from error
    if not relative.parts or ".." in relative.parts or relative.parts[0] == ".git":
        raise Held("clone", f"{path}: required project-local input")
    return relative


def require(path: Path, *, directory: bool = False) -> None:
    if not (path.is_dir() if directory else path.is_file()):
        raise Held("clone", f"{path}: missing {'directory' if directory else 'file'}")


def copy_regular(
    source: Path, destination: Path, ancestors: frozenset[Path] = frozenset(), *, immutable_objects: bool = False
) -> None:
    """Copy durable inputs from pinned link targets without destination links."""
    # Children enumerated from a pinned physical directory need only resolve
    # their own links, rather than walking every ancestor again.
    resolved = source if source.parent in ancestors and not source.is_symlink() else source.resolve(strict=True)
    if resolved in ancestors:
        raise Held("clone", f"{source}: cyclic directory link")
    if destination.is_symlink():
        destination.unlink()
    if resolved.is_dir() and immutable_objects:
        copy_generation(resolved, destination, ancestors)
    elif resolved.is_dir():
        if destination.exists() and not destination.is_dir():
            destination.unlink()
        destination.mkdir(parents=True, exist_ok=True)
        for child in resolved.iterdir():
            # Build helpers publish final files separately from these scratch
            # paths. Filter by name before opening a path that may disappear.
            if (
                child.name.startswith((".extract-", ".object-", ".input-", ".compile-"))
                and child.name != ".extract-key"
            ):
                continue
            if child.name.endswith(".partial") or child.name in {"__pycache__", ".inuse"}:
                continue
            copy_regular(child, destination / child.name, ancestors | {resolved}, immutable_objects=immutable_objects)
        shutil.copystat(resolved, destination)
    else:
        require(resolved)
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_reflink(
            resolved,
            destination,
            immutable=immutable_objects and resolved.suffix == ".o" and "assets" not in resolved.parts,
        )


def copy_generation(source: Path, destination: Path, ancestors: frozenset[Path]) -> None:
    """Materialize a pinned generation without repeating file path checks."""
    destination.mkdir(parents=True, exist_ok=True)
    ancestors = ancestors | {source}
    with os.scandir(source) as entries:
        for entry in entries:
            name = entry.name
            if name == ".inuse" or name == "__pycache__" or name.endswith(".partial"):
                continue
            if name.startswith((".extract-", ".object-", ".input-", ".compile-")) and name != ".extract-key":
                continue
            target = destination / name
            path = Path(entry.path)
            if entry.is_symlink():
                copy_regular(path, target, ancestors, immutable_objects=True)
            elif entry.is_dir():
                copy_generation(path, target, ancestors)
            else:
                copy_reflink(path, target, immutable=name.endswith(".o") and "assets" not in path.parts)
    shutil.copystat(source, destination)


_reflink_available = True


def copy_reflink(source: Path, destination: Path, *, immutable: bool = False) -> None:
    """Share atomically replaced objects; use copy on write for other files."""
    global _reflink_available
    if immutable:
        # Compile and layout publish .o through .partial + replace. Binary
        # asset objects use objcopy in place and are deliberately excluded.
        try:
            os.link(source, destination)
            return
        except OSError:
            pass
    if _reflink_available:
        try:
            with source.open("rb") as original, atomic_files.stream(destination, "wb") as target:
                fcntl.ioctl(target.fileno(), 0x40049409, original.fileno())
        except OSError as error:
            if error.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV}:
                _reflink_available = False
            atomic_files.copyfile(source, destination)
        shutil.copystat(source, destination)
    else:
        atomic_files.copy2(source, destination)


def git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True)
    if result.returncode:
        raise Held("clone", f"git {' '.join(arguments)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def isolated_policy(policy: Policy, destination: Path) -> str:
    values: dict[str, object] = {field.name: getattr(policy, field.name) for field in fields(policy)}
    values.update(cache_root=destination / ".unbake/cache", state_root=destination / ".unbake/state")
    return "".join(
        f"{name} = {json.dumps(str(value) if isinstance(value, Path) else value)}\n" for name, value in values.items()
    )


def refresh_checksums(project: Project) -> None:
    """Refresh helper hashes while retaining verified compiler pins."""
    manifest = project.tools / "compiler.sha256"
    rows = []
    for line in manifest.read_text().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise Held("clone", f"{manifest}: invalid checksum entry {line!r}")
        expected, name = parts
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise Held("clone", f"{manifest}: required project-relative checksum input {name}")
        path = project.root / relative
        require(path)
        actual = compiler_files.sha(path)
        if path.parent != project.tools and actual != expected.lower():
            raise Held("clone", f"{path}: compiler checksum expected {expected}, found {actual}")
        rows.append(f"{actual}  {name}\n")
    atomic_files.text(manifest, "".join(rows))


def rebind_evidence(path: Path, replacements: dict[bytes, bytes]) -> tuple[str, str]:
    """Stream receipt substitutions, retaining shared storage for unchanged bytes."""
    pattern = re.compile(b"|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)))
    overlap = max(map(len, replacements)) - 1
    before, after = hashlib.sha256(), hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".identity-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with path.open("rb") as original, atomic_files.stream(temporary, "wb") as output:
            pending = b""
            while block := original.read(1024 * 1024):
                before.update(block)
                pending += block
                boundary = max(0, len(pending) - overlap)
                cursor = 0
                for match in pattern.finditer(pending):
                    if match.start() >= boundary:
                        break
                    content = pending[cursor : match.start()] + replacements[match[0]]
                    output.write(content)
                    after.update(content)
                    cursor = match.end()
                boundary = max(boundary, cursor)
                content = pending[cursor:boundary]
                output.write(content)
                after.update(content)
                pending = pending[boundary:]
            content = pattern.sub(lambda match: replacements[match[0]], pending)
            output.write(content)
            after.update(content)
        old, new = before.hexdigest(), after.hexdigest()
        if old != new:
            shutil.copystat(path, temporary)
            atomic_files.publish(temporary, path)
        return old, new
    finally:
        temporary.unlink(missing_ok=True)


def copy_evidence(project: Project, cloned: Project, published_root: Path | None = None) -> None:
    """Carry atomic analysis publications and rebind their receipt dependency chain.

    Substitute only existing pins, rather than recomputing inputs: a stale
    source remains stale. Shards and unchanged indexes retain their hardlinks.
    """
    final_root = published_root or cloned.root
    replacements = {
        project.workspace_id.encode(): cloned.workspace_id.encode(),
        json.dumps(str(project.root))[1:-1].encode(): json.dumps(str(final_root))[1:-1].encode(),
    }
    old_config = compiler_files.sha(project.root / "config.toml")
    replacements[old_config.encode()] = compiler_files.sha(cloned.root / "config.toml").encode()
    receipts: list[Path] = []
    for name in ("setup", "map", "types", "layout"):
        directory = project.build / name
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.name.startswith(".") or path.name.endswith(".partial") or not path.is_file():
                continue
            if name == "setup" and path.name in setup._TRANSIENT_EVIDENCE:
                continue
            target = cloned.build / name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_reflink(path.resolve(strict=True), target, immutable=True)
            if path.suffix == ".json":
                receipts.append(target)

    # Setup layout and published feedback are inputs to map/types. Facts are
    # pinned by the database, whose digest is pinned by summary/redraft marks.
    def order(path: Path) -> tuple[int, str]:
        if path.parent.name == "setup":
            rank = 0
        elif path.name == "proven.json":
            rank = 1
        elif path.parent.name == "map":
            rank = 2 if path.name != "facts.json" else 3
        elif path.name == "database.json":
            rank = 4
        else:
            rank = 5
        return rank, str(path)

    for path in sorted(receipts, key=order):
        old, new = rebind_evidence(path, replacements)
        if old != new:
            replacements[old.encode()] = new.encode()


def create(project: Project, policy: Policy, destination: Path, versions: Sequence[str]) -> Project:
    """Publish only a ready checkout; pin warm source generations through the copy."""
    destination = destination.expanduser().absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise Held("clone", f"{destination}: destination has a symlink component")
    destination = destination.resolve()
    if destination.exists():
        raise Held("clone", f"{destination}: destination already exists")
    if destination.is_relative_to(project.root) or project.root.is_relative_to(destination):
        raise Held("clone", f"{destination}: destination overlaps source project")
    if not versions:
        raise Held("clone", "--version: required nonempty selection")
    if len(set(versions)) != len(versions):
        raise Held("clone", "--version: duplicate VERSION")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as pins:
        generations = {}
        for name in versions:
            generation = pins.enter_context(build.pin_current(project, name))
            if generation.parent != project.build or not re.fullmatch(re.escape(name) + r"\.\d+", generation.name):
                raise Held("clone", f"{generation}: required build/{name}.N generation")
            generations[name] = generation
        with tempfile.TemporaryDirectory(prefix=".clone-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "project"
            cloned = _create(project, policy, stage, versions, generations, published_root=destination)
            policy_path = cloned.tools / "clone-policy.toml"
            atomic_files.text(policy_path, isolated_policy(policy, destination))
            final_policy = destination / policy_path.relative_to(stage)
            atomic_files.text(stage / ".unbake/env", f"export UNBAKE_POLICY={shlex.quote(str(final_policy))}\n")
            # rename cannot expose a partial checkout. Refuse an intervening creator.
            if destination.exists() or destination.is_symlink():
                raise Held("clone", f"{destination}: destination already exists")
            stage.rename(destination)
    return config.load(destination)


def _create(
    project: Project,
    policy: Policy,
    destination: Path,
    versions: Sequence[str],
    generations: dict[str, Path],
    *,
    published_root: Path | None = None,
) -> Project:
    """Clone Git history, then reflink live build inputs and receipts."""
    destination = destination.expanduser().absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise Held("clone", f"{destination}: destination has a symlink component")
    destination = destination.resolve()
    if destination.exists():
        raise Held("clone", f"{destination}: destination already exists")
    if destination.is_relative_to(project.root) or project.root.is_relative_to(destination):
        raise Held("clone", f"{destination}: destination overlaps source project")
    if not versions:
        raise Held("clone", "--version: required nonempty selection")
    if len(set(versions)) != len(versions):
        raise Held("clone", "--version: duplicate VERSION")
    inputs = [project.src, project.tools, *project.include]
    for path in inputs:
        project_relative(project, path)
        require(path, directory=True)
    require(project.root / "Makefile")
    require(project.tools / "compiler.sha256")
    for name in versions:
        version = project.version(name)
        for path in (version.baserom, version.split, version.symbols):
            project_relative(project, path)
            require(path)
        with version.baserom.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha1").hexdigest()
        if actual != version.baserom_sha1:
            raise Held("clone", f"{version.baserom}: sha1 expected {version.baserom_sha1}, found {actual}")
        generation = generations[name]
        if generation.parent != project.build or not re.fullmatch(re.escape(name) + r"\.\d+", generation.name):
            raise Held("clone", f"{generation}: required build/{name}.N generation")
        for filename in (".split.mk", ".extract-key", project.name + ".elf", f"{project.name}.{name}.z64"):
            require(generation / filename)
        assembly = project.asm / name
        project_relative(project, assembly)
        require(assembly, directory=True)
        generations[name] = generation
    tracked = git(project.root, "ls-files", "-z").decode().split("\0")
    for name in filter(None, tracked):
        require(project.root / name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--no-hardlinks", "--", str(project.root), str(destination)], capture_output=True, text=True
    )
    if result.returncode:
        raise Held("clone", f"git clone: {result.stderr.strip()}")
    # Materialize checkout links first, so nested copies cannot traverse them.
    for child in destination.iterdir():
        if child.name != ".git":
            materialize_links(child)
    for name in filter(None, tracked):
        copy_regular(project.root / name, destination / name)
    # Init and setup deliberately make no initial commit. Durable ready inputs
    # must also be copied when the shell still has an empty Git index/history.
    for name in ("config.toml", "Makefile", "README.md", "CONTRIBUTING.md", ".gitignore", "docs"):
        path = project.root / name
        if path.exists():
            copy_regular(path, destination / name)
    for path in inputs:
        copy_regular(path, destination / project_relative(project, path))
    for name, generation in generations.items():
        version = project.version(name)
        for path in (version.baserom, version.split.parent, version.symbols, project.asm / name):
            copy_regular(path, destination / project_relative(project, path))
        target = destination / project.build.relative_to(project.root) / generation.name
        copy_regular(generation, target, immutable_objects=True)
        (target / ".inuse").touch()
        shutil.copystat(generation, target)
        link = destination / project.build.relative_to(project.root) / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            shutil.rmtree(link)
        link.symlink_to(target.name, target_is_directory=True)
    config_path = destination / "config.toml"
    from unbake.project.setup_config import strip_workspace

    atomic_files.text(config_path, strip_workspace(config_path.read_text()))
    atomic_files.text(destination / ".unbake/workspace-id", str(uuid4()) + "\n")
    cloned = config.load(destination)
    copy_evidence(project, cloned, published_root)
    for path in (cloned.src, cloned.tools, cloned.asm, *cloned.include):
        project_relative(cloned, path)
    policy_path = cloned.tools / "clone-policy.toml"
    if policy_path.is_symlink():
        policy_path.unlink()
    atomic_files.text(policy_path, isolated_policy(policy, destination))
    local_policy = project_relative(cloned, policy_path).as_posix()
    # The existing Makefile includes these ignored, clone-local build graphs.
    # Keep policy selection out of every tracked game file.
    for generation in generations.values():
        graph = destination / project.build.relative_to(project.root) / generation.name / ".split.mk"
        original_stat = graph.stat()
        with tempfile.NamedTemporaryFile(dir=graph.parent, prefix=".graph-", delete=False) as output:
            temporary = Path(output.name)
        try:
            with atomic_files.stream(temporary, "wb") as output, graph.open("rb") as original:
                output.write(
                    (
                        f"export UNBAKE_POLICY := $(abspath {local_policy})\n"
                        "DRIVERS := $(filter-out $(TOOLS)/cache.py,$(DRIVERS))\n"
                    ).encode()
                )
                shutil.copyfileobj(original, output)
            shutil.copystat(graph, temporary)
            atomic_files.publish(temporary, graph)
        finally:
            temporary.unlink(missing_ok=True)
        os.utime(graph, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        shutil.copystat(generation, graph.parent)
    local = destination / ".unbake"
    local.mkdir(exist_ok=True)
    # Operators can source this environment for setup, clean builds and CLI use.
    atomic_files.text(local / "env", f"export UNBAKE_POLICY={shlex.quote(str(policy_path))}\n")
    atomic_files.text(destination / ".gitignore", hygiene.ignore_text(cloned))
    if prepare(cloned, config.load_policy(policy_path)):
        # A replaced compiler invalidates warm code even when its path/flags
        # stay identical. Cache service updates alone do not affect object bytes.
        for generation in generations.values():
            for receipt in (destination / project.build.relative_to(project.root) / generation.name).rglob("*.built"):
                receipt.unlink()
    refresh_checksums(cloned)
    return cloned


def prepare(project: Project, policy: Policy) -> bool:
    """Acquire pins and current build helpers in the clone, never in its source."""
    changed = False
    for ident in project.compilers:
        spec = toolchain.specification(ident)
        for name, pin in spec.pins.items():
            path = project.tools / ident / name
            if not path.is_file() or compiler_files.sha(path) != pin:
                changed = True
    if changed:
        toolchain.ensure(project, policy)
    helpers = makefile.helpers(project)
    # Retain the proved helpers and their timestamps in a complete warm copy.
    # A partial compiler/tool fixture still needs its missing build drivers.
    if any(not (project.root / name).is_file() for name in helpers):
        setup.publish_files(project, helpers)
    else:
        # Upgrade all publication helpers before sharing build objects. Their
        # timestamps are not object recipe inputs; unchanged bytes stay warm.
        for name, content in makefile.helper_sources(project).items():
            path = project.root / name
            original_stat = path.stat()
            compiler_files.atomic_bytes(path, content.encode(), mode=original_stat.st_mode & 0o777)
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    return changed


def materialize_links(path: Path) -> None:
    if path.is_symlink():
        target = path.resolve(strict=True)
        path.unlink()
        copy_regular(target, path)
    elif path.is_dir():
        for child in path.iterdir():
            materialize_links(child)
