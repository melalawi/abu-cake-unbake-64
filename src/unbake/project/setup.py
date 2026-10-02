"""Verify supplied project inputs before publishing standalone build files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unbake.project import build, compiler_files, config, hygiene, makefile, setup_config, setup_proof, toolchain
from unbake.project.config import Held, PendingProject, Policy, Project, SetupPolicy
from unbake.project_tools.host import resolve_tool

if TYPE_CHECKING:
    from unbake.project.census import Census
    from unbake.project.flow import CompilerProposal, LayoutManifest


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


def run(project: Project, policy: Policy | SetupPolicy, *, supply: Path | None = None) -> list[str]:
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
    for compiler in project.compilers.values():
        if str(compiler.as_).startswith("policy:"):
            try:
                resolve_tool(str(compiler.as_))
            except ValueError as error:
                raise Held("setup", str(error)) from error
    files = makefile.render(project)
    files[".gitignore"] = hygiene.ignore_text(project)
    toolchain.ensure(project, policy, supply=supply)
    verify_compiler(project)
    receipts = []
    for name in project.versions:
        version = project.version(name)
        rom = version.baserom
        rom = project.root / rom
        rom_content = _read(rom)
        digest = hashlib.sha1(rom_content).hexdigest()
        if digest != version.baserom_sha1:
            value = "baserom_sha1"
            raise Held("setup", f"{rom}: [version.{name}].{value} expected {version.baserom_sha1}, got {digest}")
        split = project.root / version.split
        text = _read(split).decode()
        for number, line in enumerate(text.splitlines(), 1):
            if _comment(line):
                raise Held("setup", f"{split}:{number}: split YAML comment")
        _read(project.root / version.symbols)
        files[f"versions/{name}/baserom.sha1"] = f"{digest}  {version.baserom.relative_to(project.root).as_posix()}\n"
        files[f"versions/{name}/{project.name}.sha1"] = (
            f"{digest}  {project.build.relative_to(project.root).as_posix()}/{name}/{project.name}.{name}.z64\n"
        )
        receipts.append(f"OK(setup): {name}: baserom and compiler verified; standalone build rendered")
    if config_text != config_path.read_text():
        compiler_files.atomic_bytes(config_path, config_text.encode())
    publish_files(project, files)
    return receipts


def publish_files(project: Project, files: dict[str, str]) -> None:
    """Publish generated build files and refresh only tool-owned manifest entries."""
    for relative, content in files.items():
        destination = project.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.read_text() != content:
            compiler_files.atomic_bytes(destination, content.encode())
    manifest = project.tools / "compiler.sha256"
    generated = set(files)
    # The manifest identifies generated helpers that the tool owns.
    for row in manifest.read_text().splitlines():
        fields = row.split(maxsplit=1)
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
            continue
        obsolete = Path(fields[1])
        if (
            obsolete.parent == project.tools.relative_to(project.root)
            and obsolete.suffix == ".py"
            and str(obsolete) not in generated
        ):
            target = project.root / obsolete
            if target.is_file() or target.is_symlink():
                target.unlink()
    pins = "".join(
        line + "\n"
        for line in manifest.read_text().splitlines()
        if len(line.split(maxsplit=1)) != 2
        or (
            line.split(maxsplit=1)[1] not in generated
            and (
                Path(line.split(maxsplit=1)[1]).parent != project.tools.relative_to(project.root)
                or Path(line.split(maxsplit=1)[1]).suffix != ".py"
            )
        )
    )
    for relative in files:
        if relative.startswith(str(project.tools.relative_to(project.root)) + "/"):
            digest = hashlib.sha256((project.root / relative).read_bytes()).hexdigest()
            pins += f"{digest}  {relative}\n"
    if manifest.read_text() != pins:
        compiler_files.atomic_bytes(manifest, pins.encode())


def _inputs(project: PendingProject | Project) -> dict[str, str]:
    """Pin every repository input, including human documents, during the proof."""
    result = {}
    excluded = (project.build, project.asm, project.root / ".git", project.root / ".splat")
    for directory, names, files in os.walk(project.root):
        parent = Path(directory)
        for name in names:
            path = parent / name
            if path.is_symlink() and not any(path.is_relative_to(output) for output in excluded):
                raise Held("setup", f"setup.publication: input directory symlink {path.relative_to(project.root)}")
        names[:] = [
            name
            for name in names
            if not any((parent / name).is_relative_to(path) for path in excluded) and name != "__pycache__"
        ]
        for name in files:
            path = parent / name
            if path.is_symlink():
                raise Held("setup", f"setup.publication: input symlink {path.relative_to(project.root)}")
            result[path.relative_to(project.root).as_posix()] = hashlib.sha256(_read(path)).hexdigest()
    return result


def _copy_inputs(project: PendingProject | Project, tree: Path, fingerprint: dict[str, str]) -> None:
    tree.mkdir()
    for relative in fingerprint:
        destination = tree / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project.root / relative, destination)


def _seed_generations(project: Project, staged: Project) -> None:
    """Copy pinned build caches; standalone make still proves every staged ROM."""
    with build.lock(project):
        for version in project.versions:
            link = project.build_link(version)
            if not link.is_symlink() or not (link / ".split.mk").is_file():
                continue
            current = build.current_generation(project, version)
            with build.pin(current):
                destination = staged.build / f"{version}.0"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(current, destination, ignore=shutil.ignore_patterns(".inuse", "*.log"))
                _relocate_generation(destination, project.root, staged.root)
                staged.build_link(version).symlink_to(destination.name)
        if project.asm.is_dir():
            shutil.copytree(project.asm, staged.asm)


def _relocate_generation(generation: Path, source: Path, destination: Path) -> None:
    for path in generation.rglob("*"):
        if path.is_file() and path.suffix in {".mk", ".d", ".ld", ".json", ".txt", ".flags"}:
            content = path.read_bytes()
            changed = content.replace(str(source).encode(), str(destination).encode())
            if content != changed:
                path.write_bytes(changed)


def _write(tree: Path, relative: str, content: str) -> None:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise Held("setup", f"setup.publication: invalid output path {relative}")
    destination = tree / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)


def _layout_inputs(project: PendingProject, census: Census, layout: LayoutManifest, tree: Path) -> None:
    from unbake.layout import planner

    for relative, content in planner.render_layout(project, census, layout).items():
        _write(tree, relative, content)
    _write(tree, "docs/setup/layout.json", json.dumps(layout, indent=2, sort_keys=True) + "\n")


def _build_options(layout: LayoutManifest, policy: SetupPolicy) -> dict[str, Any]:
    return {
        "ld": "policy:mips_ld",
        "objcopy": "policy:mips_objcopy",
        "splat": "policy:splat",
        "as": "policy:mips_as",
        "cpp": "policy:cpp",
        "asflags": list(policy.asflags),
        "cppflags": list(policy.cppflags),
        "sn64_asflags": list(policy.sn64_asflags),
        "resident_mappings": {
            version: record["evidence"].get("resident_mappings", []) for version, record in layout["versions"].items()
        },
    }


def layout_receipts(layout: LayoutManifest) -> list[str]:
    lines = []
    for version, record in layout["versions"].items():
        groups = []
        for kind in ("text", "private", "shared", "writable", "unresolved", "bin"):
            providers = [provider for provider in record["providers"] if provider["kind"] == kind]
            count = len(providers)
            size = sum(provider["end"] - provider["start"] for provider in providers)
            groups.append(f"{kind}={count} ({size} bytes)")
        lines.append(f"layout {version}: " + "; ".join(groups))
        for provider in record["providers"]:
            if provider["kind"] == "shared":
                lines.append(f"shared provider {version}:{provider['name']}: owners={','.join(provider['owners'])}")
    return lines


def _ready_readme(project: PendingProject | Project, census: Census, layout: LayoutManifest, tree: Path) -> None:
    from unbake.project import init
    from unbake.project.header import DESTINATIONS
    from unbake.report.progress import render

    readme = project.root / "README.md"
    if not readme.is_file() or readme.read_bytes() != init.readme_text(project.root).encode():
        return
    facts = setup_config.facts(config.load_pending(project.root), census, name=None, title=None)
    descriptions = []
    reports = {}
    for cartridge in census.cartridges:
        version = census.names[cartridge.path]
        descriptions.append(
            f"| {version} ({DESTINATIONS[cartridge.header.region]}, revision {cartridge.header.revision}) |"
        )
        functions = layout["versions"][version]["functions"]
        reports[version] = {
            "version": 2,
            "measures": {
                "complete_code": 0,
                "total_code": sum(function["end"] - function["start"] for function in functions),
                "complete_units": 0,
                "total_units": len(functions),
            },
        }
    template = (makefile.TEMPLATES / "README.ready.md").read_text()
    template = template.replace("@TITLE@", facts["project"]["title"])
    template = template.replace("@PROGRESS@", "\n\n".join(descriptions))
    _write(tree, "README.md", render(template, reports))


def _sdk_headers(project: Project) -> None:
    """Install the open macro asset and one shared SDK display-list type."""
    from unbake.decomp.gbi_source import gfx_typedefs

    root = project.include[0]
    shared_type = root / "shared/gfx.h"
    for directory in project.include:
        if directory.is_dir():
            for path in directory.rglob("*.h"):
                if path != shared_type:
                    spans = gfx_typedefs(path.read_text())
                    if spans:
                        line = path.read_text()[: spans[0][0]].count("\n") + 1
                        raise Held(
                            "setup", f"setup.gfx_type: {path.relative_to(project.root)}:{line}: duplicate SDK Gfx"
                        )
    files = {
        "shared/gfx.h": (
            '#ifndef UNBAKE_SHARED_GFX_H\n#define UNBAKE_SHARED_GFX_H\n#include "types.h"\n\n'
            "typedef union Gfx {\n"
            "    struct { u32 w0; u32 w1; } words;\n"
            "    u64 force_structure_alignment;\n"
            "} Gfx;\n\n#endif\n"
        ),
        "n64sdk.h": '#ifndef UNBAKE_N64SDK_H\n#define UNBAKE_N64SDK_H\n#include "shared/gfx.h"\n#endif\n',
        "gbi.h": (makefile.TEMPLATES / "gbi.h").read_text(),
    }
    for name, content in files.items():
        target = root / name
        if target.exists() and target.read_bytes() != content.encode():
            key = "setup.gbi_header" if name == "gbi.h" else "setup.gfx_type"
            raise Held(
                "setup", f"{key}: {target.relative_to(project.root)}: existing header differs; preserve human input"
            )
    for name, content in files.items():
        target = root / name
        if not target.exists():
            _write(project.root, target.relative_to(project.root).as_posix(), content)


def _publish(
    project: PendingProject | Project,
    staged: Project,
    fingerprint: dict[str, str],
    *,
    fresh: bool,
    generations: dict[str, str | None],
) -> None:
    """Publish proved files and generations under the shared lock; config is last."""
    writes: dict[Path, tuple[bytes, int, int]] = {}
    staged_inputs = _inputs(staged)
    for relative in staged_inputs:
        path = staged.root / relative
        if path.is_relative_to(staged.roms) and (project.root / relative).exists():
            continue
        shell_readme = relative == "README.md" and _read(project.root / relative) != path.read_bytes()
        if (
            not fresh
            and not shell_readme
            and (
                path.is_relative_to(staged.src)
                or (
                    any(path.is_relative_to(directory) for directory in staged.include)
                    and (project.root / relative).exists()
                )
                or (path.is_relative_to(staged.root / "versions") and path.suffix not in {".sha1"})
                or relative in {"README.md", "CONTRIBUTING.md", "config.toml", ".gitignore"}
                or relative.startswith("docs/")
            )
        ):
            continue
        target = project.root / relative
        if not target.exists() or target.read_bytes() != path.read_bytes():
            writes[target] = (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
    # Extracted assembly is a generated, ignored input for standalone make.
    if staged.asm.is_dir():
        for path in staged.asm.rglob("*"):
            if path.is_file():
                target = project.asm / path.relative_to(staged.asm)
                content = path.read_bytes()
                if not target.exists() or target.read_bytes() != content:
                    writes[target] = (content, path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
    config_path = project.root / "config.toml"
    if fresh:
        staged_config = staged.root / "config.toml"
        writes[config_path] = (staged_config.read_bytes(), 0o644, staged_config.stat().st_mtime_ns)
    obsolete = []
    manifest = project.tools / "compiler.sha256"
    if manifest.is_file():
        for row in manifest.read_text().splitlines():
            fields = row.split(maxsplit=1)
            if len(fields) == 2 and re.fullmatch(r"[0-9a-f]{64}", fields[0]):
                obsolete_path = Path(fields[1])
                if (
                    not obsolete_path.is_absolute()
                    and ".." not in obsolete_path.parts
                    and obsolete_path.is_relative_to(project.tools.relative_to(project.root))
                    and obsolete_path.as_posix() in fingerprint
                    and obsolete_path.as_posix() not in staged_inputs
                ):
                    obsolete.append(project.root / obsolete_path)
    project.build.mkdir(parents=True, exist_ok=True)
    before: dict[Path, tuple[bytes, int, int] | None] = {}
    previous_links: dict[Path, str | None] = {}
    moved: list[Path] = []
    directories: list[Path] = []
    with build._lock(project.build / ".lock"):
        if _inputs(project) != fingerprint:
            raise Held("setup", "setup.publication: project inputs changed during proof")
        if _generations(project, staged.versions) != generations:
            raise Held("setup", "setup.publication: current generations changed during proof")
        try:
            for target in [*writes, *obsolete]:
                if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                    raise Held("setup", f"setup.publication: output symlink {target}")
                before[target] = (
                    (target.read_bytes(), target.stat().st_mode & 0o777, target.stat().st_mtime_ns)
                    if target.exists()
                    else None
                )
                parent = target.parent
                while not parent.exists():
                    directories.append(parent)
                    parent = parent.parent
            for version in staged.versions:
                link = project.build / version
                if link.exists() and not link.is_symlink():
                    raise Held("setup", f"setup.publication: generation link {link}: expected symlink")
                previous_links[link] = os.readlink(link) if link.is_symlink() else None
                number = 0
                while (project.build / f"{version}.{number}").exists() or (
                    project.build / f"{version}.{number}"
                ).is_symlink():
                    number += 1
                destination = project.build / f"{version}.{number}"
                os.replace(staged.build_link(version).resolve(strict=True), destination)
                moved.append(destination)
                # Extraction dependencies normally use project-relative paths.
                # Relocate explicit temporary paths in generated text receipts.
                _relocate_generation(destination, staged.root, project.root)
            for target, (content, mode, mtime) in writes.items():
                if target != config_path:
                    compiler_files.atomic_bytes(target, content, mode=mode)
                    os.utime(target, ns=(target.stat().st_atime_ns, mtime))
            for target in obsolete:
                target.unlink()
            for version, generation in zip(staged.versions, moved, strict=True):
                _swap(project.build / version, generation.name)
            if config_path in writes:
                content, mode, mtime = writes[config_path]
                compiler_files.atomic_bytes(config_path, content, mode=mode)
                os.utime(config_path, ns=(config_path.stat().st_atime_ns, mtime))
        except BaseException:
            for link, previous in previous_links.items():
                if previous is None:
                    link.unlink(missing_ok=True)
                else:
                    _swap(link, previous)
            for target, previous_file in before.items():
                if previous_file is None:
                    target.unlink(missing_ok=True)
                else:
                    content, mode, mtime = previous_file
                    compiler_files.atomic_bytes(target, content, mode=mode)
                    os.utime(target, ns=(target.stat().st_atime_ns, mtime))
            for generation in moved:
                shutil.rmtree(generation)
            for directory in sorted(set(directories), key=lambda path: len(path.parts), reverse=True):
                if directory.exists() and not any(directory.iterdir()):
                    directory.rmdir()
            raise


def _swap(link: Path, target: str) -> None:
    temporary = link.with_name(f".{link.name}.setup")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def _generations(project: PendingProject | Project, versions: tuple[str, ...]) -> dict[str, str | None]:
    return {
        version: str((project.build / version).resolve()) if (project.build / version).is_symlink() else None
        for version in versions
    }


def _prove_publish(
    project: PendingProject | Project,
    tree: Path,
    policy: SetupPolicy,
    fingerprint: dict[str, str],
    *,
    fresh: bool,
    supply: Path | None,
    before_publish: Callable[[], None] | None = None,
) -> list[str]:
    staged = config.load(tree)
    generations = _generations(project, staged.versions)
    contributing = tree / "CONTRIBUTING.md"
    previous = contributing.read_bytes() if contributing.exists() and not fresh else None
    run(staged, policy, supply=supply)
    _sdk_headers(staged)
    if previous is not None:
        contributing.write_bytes(previous)
    workers = min(policy.cores, len(staged.versions))
    cores = max(1, policy.cores // workers)

    def prove_version(version: str) -> str:
        data = staged.version(version).baserom.read_bytes()
        log = project.build / "setup/logs" / f"{version}.log"
        setup_proof.proof(staged, version, data, cores, log=log)
        digest = hashlib.sha1(data).hexdigest()
        return f"{version}: SHA1 {digest}; every cartridge byte proved"

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(prove_version, version) for version in staged.versions]
        receipts = [future.result() for future in futures]
    if before_publish is not None:
        before_publish()
    _publish(project, staged, fingerprint, fresh=fresh, generations=generations)
    return [*receipts, "ready: confirmed configuration and proved generations published"]


def complete_setup(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    proposal: CompilerProposal,
    policy: SetupPolicy,
    *,
    confirm: str | None = None,
    supply: Path | None = None,
) -> list[str]:
    """Confirm exact evidence, stage full inputs, prove every ROM, publish readiness."""
    from unbake.project import fingerprint as compilers

    for line in compilers.receipt(proposal):
        print(f"OK(setup): {line}")
    compilers.confirm_proposal(project, census, layout, proposal, policy, confirm=confirm)
    accepted = _read(project.build / "setup/proposal.json")
    if proposal["default_compiler"] is None:
        raise Held("setup", "setup.compiler_candidate: explicit default compiler required")
    facts = setup_config.facts(project, census, name=None, title=None)
    fingerprint = _inputs(project)
    directory = project.build / "setup"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="proof-", dir=directory) as temporary:
        tree = Path(temporary) / "tree"
        _copy_inputs(project, tree, fingerprint)
        _layout_inputs(project, census, layout, tree)
        _ready_readme(project, census, layout, tree)
        _write(
            tree,
            "config.toml",
            setup_config.render_ready(
                project,
                census,
                name=facts["project"]["name"],
                title=facts["project"]["title"],
                default_compiler=proposal["default_compiler"],
                assignments=proposal["assignments"],
                compiler_ties=proposal.get("compiler_ties", {}),
                cflags={ident: tuple(flags) for ident, flags in proposal["cflags"].items()},
                build=_build_options(layout, policy),
            ),
        )
        _write(tree, "docs/setup/compiler.json", accepted.decode("utf-8"))
        _write(
            tree,
            "docs/setup/confirmation.json",
            json.dumps({"proposal_sha256": hashlib.sha256(accepted).hexdigest()}) + "\n",
        )
        pending = config.load_pending(tree)
        pending.src.mkdir(parents=True, exist_ok=True)
        types = pending.include[0] / "types.h"
        if not types.exists():
            _write(
                tree,
                types.relative_to(tree).as_posix(),
                "#ifndef UNBAKE_TYPES_H\n#define UNBAKE_TYPES_H\n"
                "typedef signed char s8;\ntypedef unsigned char u8;\n"
                "typedef signed short s16;\ntypedef unsigned short u16;\n"
                "typedef signed int s32;\ntypedef unsigned int u32;\n"
                "typedef signed long long s64;\ntypedef unsigned long long u64;\n"
                "typedef float f32;\ntypedef double f64;\n#endif\n",
            )
        return _prove_publish(
            project,
            tree,
            policy,
            fingerprint,
            fresh=True,
            supply=supply,
            before_publish=lambda: compilers.confirm_proposal(
                project, census, layout, proposal, policy, confirm=hashlib.sha256(accepted).hexdigest()
            ),
        )


def refresh(project: Project, policy: SetupPolicy, *, supply: Path | None = None) -> list[str]:
    """Prove existing authored inputs and refresh only generated files."""
    from unbake.project import census

    fingerprint = _inputs(project)
    directory = project.build / "setup"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="proof-", dir=directory) as temporary:
        tree = Path(temporary) / "tree"
        _copy_inputs(project, tree, fingerprint)
        staged = config.load(tree)
        _seed_generations(project, staged)
        if supply is not None:
            restore_roms(staged, supply)
        manifest = project.build / "setup/roms.json"
        if manifest.is_file():
            destination = staged.build / "setup/roms.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest, destination)
            measured = census.run(config.load_pending(tree), policy, names_from=project.names_from)
            layout_path = tree / "docs/setup/layout.json"
            if layout_path.is_file():
                _ready_readme(project, measured, cast("LayoutManifest", json.loads(layout_path.read_bytes())), tree)
        return _prove_publish(project, tree, policy, fingerprint, fresh=False, supply=supply)
