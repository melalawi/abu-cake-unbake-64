"""Verify supplied project inputs before publishing standalone build files."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unbake import atomic as atomic_files
from unbake import config, tui
from unbake.compilers import files as compiler_files
from unbake.compilers import registry as toolchain
from unbake.config import Held, Host, PendingProject, Project
from unbake.project import hygiene, setup_config

_AUDIO_CALLBACKS = "audio_callbacks.h"
TEMPLATES = Path(__file__).parents[1] / "templates"

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
    contents = compiler_files.directory_paths(source, {version.baserom_sha1 for version in missing}, "sha1")
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
        compiler_files.atomic_copy(target, contents[version.baserom_sha1], mode=0o600)


def run(project: Project, policy: Host, *, supply: Path | None = None) -> list[str]:
    """Install and verify compilers, check every original ROM, write .gitignore and the build files."""
    from unbake import buildfiles

    toolchain.ensure(project, policy, supply=supply)
    verify_compiler(project)
    receipts = []
    for name in project.versions:
        version = project.version(name)
        rom_content = _read(version.baserom)
        digest = hashlib.sha1(rom_content).hexdigest()
        if digest != version.baserom_sha1:
            raise Held(
                "setup",
                f"{version.baserom}: [version.{name}].baserom_sha1 expected {version.baserom_sha1}, got {digest}",
            )
        text = _read(version.split).decode()
        for number, line in enumerate(text.splitlines(), 1):
            if _comment(line):
                raise Held("setup", f"{version.split}:{number}: split YAML comment")
        _read(version.symbols)
        receipts.append(f"{name}: baserom and compilers verified")
    ignore = hygiene.ignore_text(project)
    if not (project.root / ".gitignore").is_file() or (project.root / ".gitignore").read_text() != ignore:
        atomic_files.text(project.root / ".gitignore", ignore)
    buildfiles.write(project, policy)
    return receipts


def _inputs(project: PendingProject | Project) -> dict[str, str]:
    """Pin every repository input, including human documents, during the proof."""
    result = {}
    excluded = (project.build, project.root / "asm", project.root / ".git", project.root / ".splat")
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
            result[path.relative_to(project.root).as_posix()] = compiler_files.sha(path)
    return result


# Setup evidence is regenerable build output, never part of the game repository.
_EVIDENCE = "build/setup"
_RETIRED_EVIDENCE = "docs/setup/"
# Review proposals are recomputed, never inputs to another setup transaction.
_TRANSIENT_EVIDENCE = frozenset({"symbol-proposal.json", "join-proposal.json", "proposal.json"})


def _retired_evidence(relative: str) -> bool:
    return relative.startswith(_RETIRED_EVIDENCE) and relative.endswith(".json")


def _copy_inputs(project: PendingProject | Project, tree: Path, fingerprint: dict[str, str]) -> None:
    tree.mkdir()
    evidence = project.build / "setup"
    if evidence.is_dir():
        for path in evidence.iterdir():
            if path.is_file() and path.name not in _TRANSIENT_EVIDENCE:
                destination = tree / _EVIDENCE / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.copy2(path, destination)
    for relative in fingerprint:
        # Committed evidence moves to build output; publication deletes it.
        destination = tree / (
            _EVIDENCE + "/" + relative[len(_RETIRED_EVIDENCE) :] if _retired_evidence(relative) else relative
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_files.copy2(project.root / relative, destination)


def _write(tree: Path, relative: str, content: str) -> None:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise Held("setup", f"setup.publication: invalid output path {relative}")
    destination = tree / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_files.text(destination, content)


def _layout_inputs(project: PendingProject, census: Census, layout: LayoutManifest, tree: Path) -> None:
    from unbake.layout import planner

    for relative, content in planner.render_layout(project, census, layout).items():
        _write(tree, relative, content)
    _write(tree, "build/setup/layout.json", json.dumps(layout, indent=2, sort_keys=True) + "\n")


def _build_options(project: PendingProject, layout: LayoutManifest) -> dict[str, Any]:
    return {
        "asflags": list(project.asflags),
        "cppflags": list(project.cppflags),
        "sn64_asflags": list(project.sn64_asflags),
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
    if (project.root / "README.md").exists():
        return
    facts = setup_config.facts(config.load_pending(project.root), census, name=None, title=None)
    template = (TEMPLATES / "README.ready.md").read_text()
    _write(tree, "README.md", template.replace("@TITLE@", facts["project"]["title"]))


def _sdk_headers(project: Project) -> None:
    """Install the open macro asset and one shared SDK display-list type."""
    from unbake.decomp.gbi_source import gfx_typedefs

    root = project.include[0]
    shared_type = root / "gfx.h"
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
        "gfx.h": (
            '#ifndef UNBAKE_SHARED_GFX_H\n#define UNBAKE_SHARED_GFX_H\n#include "types.h"\n\n'
            "typedef union Gfx {\n"
            "    struct { u32 w0; u32 w1; } words;\n"
            "    u64 force_structure_alignment;\n"
            "} Gfx;\n\n#endif\n"
        ),
        "n64sdk.h": '#ifndef UNBAKE_N64SDK_H\n#define UNBAKE_N64SDK_H\n#include "gfx.h"\n#endif\n',
        "gbi.h": (TEMPLATES / "gbi.h").read_text(),
        "acmd.h": (TEMPLATES / "acmd.h").read_text(),
        "abi.h": (TEMPLATES / "abi.h").read_text(),
        _AUDIO_CALLBACKS: (TEMPLATES / "audio_callbacks.h").read_text(),
    }
    from unbake.decomp.gbi import PREVIOUS_HEADER_SHA256

    previous_gbi = root / "gbi.h"
    upgrade_gbi = (
        previous_gbi.is_file()
        and not previous_gbi.is_symlink()
        and hashlib.sha256(previous_gbi.read_bytes()).hexdigest() == PREVIOUS_HEADER_SHA256
    )
    for name, content in files.items():
        target = root / name
        if (
            name != _AUDIO_CALLBACKS
            and not (name == "gbi.h" and upgrade_gbi)
            and target.exists()
            and target.read_bytes() != content.encode()
        ):
            key = "setup.gbi_header" if name == "gbi.h" else "setup.gfx_type"
            raise Held(
                "setup", f"{key}: {target.relative_to(project.root)}: existing header differs; preserve human input"
            )
    for name, content in files.items():
        target = root / name
        # Callback contracts are tool-owned and restored during the staged proof.
        if (
            not target.exists()
            or (name == "gbi.h" and upgrade_gbi)
            or (name == _AUDIO_CALLBACKS and target.read_bytes() != content.encode())
        ):
            _write(project.root, target.relative_to(project.root).as_posix(), content)


def _publish(
    project: PendingProject | Project,
    staged: Project,
    fingerprint: dict[str, str],
    *,
    fresh: bool,
    removed_inputs: tuple[str, ...] = (),
) -> None:
    """Publish the proved files; config.toml is written last; any failure restores every file."""
    writes: dict[Path, Path] = {}
    staged_inputs = _inputs(staged)
    for relative in staged_inputs:
        path = staged.root / relative
        if path.is_relative_to(staged.roms) and (project.root / relative).exists():
            continue
        if not fresh and (
            path.is_relative_to(staged.src)
            or (
                any(path.is_relative_to(directory) for directory in staged.include)
                and path != staged.include[0] / _AUDIO_CALLBACKS
                and (project.root / relative).exists()
            )
            or relative in {"README.md", "CONTRIBUTING.md"}
        ):
            continue
        target = project.root / relative
        if not target.exists() or compiler_files.sha(target) != staged_inputs[relative]:
            writes[target] = path
    config_path = project.root / "config.toml"
    if fresh:
        writes[config_path] = staged.root / "config.toml"
    staged_evidence = staged.root / _EVIDENCE
    if staged_evidence.is_dir():
        for path in staged_evidence.iterdir():
            if path.is_file():
                writes[project.build / "setup" / path.name] = path
    obsolete = [project.root / relative for relative in removed_inputs]
    obsolete += [project.root / relative for relative in fingerprint if _retired_evidence(relative)]
    before: dict[Path, bytes | None] = {}
    if _inputs(project) != fingerprint:
        raise Held("setup", "setup.publication: project inputs changed during proof")
    try:
        for target in [*writes, *obsolete]:
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                raise Held("setup", f"setup.publication: output symlink {target}")
            before[target] = target.read_bytes() if target.is_file() else None
        for target, source in writes.items():
            if target != config_path:
                compiler_files.atomic_copy(target, source, mode=source.stat().st_mode & 0o777)
        for target in obsolete:
            target.unlink(missing_ok=True)
        if config_path in writes:
            compiler_files.atomic_bytes(config_path, writes[config_path].read_bytes())
    except BaseException:
        for target, previous in before.items():
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                compiler_files.atomic_bytes(target, previous)
        raise


def _prove_publish(
    project: PendingProject | Project,
    tree: Path,
    policy: Host,
    fingerprint: dict[str, str],
    *,
    fresh: bool,
    supply: Path | None,
    before_publish: Callable[[], None] | None = None,
    removed_inputs: tuple[str, ...] = (),
) -> list[str]:
    """Set up the staged tree, prove it with plain `make check`, then publish it."""
    from unbake import build

    staged = config.load(tree)
    contributing = tree / "CONTRIBUTING.md"
    previous = contributing.read_bytes() if contributing.exists() and not fresh else None
    receipts = run(staged, policy, supply=supply)
    _sdk_headers(staged)
    if previous is not None:
        atomic_files.write(contributing, previous)
    outcome = build.check(staged, policy)
    if not outcome.ok:
        raise Held("setup", "setup.proof: make check failed on the staged project:\n" + "\n".join(outcome.lines()))
    if before_publish is not None:
        before_publish()
    _publish(project, staged, fingerprint, fresh=fresh, removed_inputs=removed_inputs)
    return [*receipts, *outcome.lines(), "ready: every version rebuilt byte-identical; files published"]


@contextmanager
def prepare_setup(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    proposal: CompilerProposal,
    policy: Host,
    *,
    confirm: str | None = None,
    supply: Path | None = None,
) -> Iterator[Callable[[], list[str]]]:
    """Stage confirmed inputs, then release planning state before the proof."""
    from unbake.compilers import fingerprint as compilers
    from unbake.compilers import propose as compiler_proposal

    for line in compilers.receipt(proposal):
        tui.line(f"OK(setup): {line}")
    compilers.confirm_proposal(project, census, layout, proposal, policy, confirm=confirm)
    accepted_path = project.build / "setup/proposal.json"
    accepted_sha256 = compiler_files.sha(accepted_path)
    guard = compiler_proposal.confirmation_guard(project, proposal, policy)
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
                cflags={ident: tuple(flags) for ident, flags in proposal["cflags"].items()},
                build=_build_options(project, layout),
            ),
        )
        compiler_document = tree / "build/setup/compiler.json"
        compiler_document.parent.mkdir(parents=True, exist_ok=True)
        atomic_files.copy2(accepted_path, compiler_document)
        _write(
            tree,
            "build/setup/confirmation.json",
            json.dumps({"proposal_sha256": accepted_sha256}) + "\n",
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
        del census, layout, proposal

        def prove() -> list[str]:
            result = _prove_publish(project, tree, policy, fingerprint, fresh=True, supply=supply, before_publish=guard)
            # compiler.json and confirmation.json hold the published receipt.
            # Do not delete a proposal from an intervening planning command.
            if accepted_path.is_file() and compiler_files.sha(accepted_path) == accepted_sha256:
                accepted_path.unlink()
            return result

        yield prove


def complete_setup(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    proposal: CompilerProposal,
    policy: Host,
    *,
    confirm: str | None = None,
    supply: Path | None = None,
) -> list[str]:
    """Stage and prove supplied setup facts in one transaction."""
    with prepare_setup(project, census, layout, proposal, policy, confirm=confirm, supply=supply) as prove:
        del census, layout, proposal
        return prove()


def refresh(pending: PendingProject, policy: Host, *, supply: Path | None = None) -> list[str]:
    """Prove existing authored inputs, refresh generated files and keep config.toml configuration only."""
    from unbake.project import census

    configured = setup_config.canonical(_read(pending.root / "config.toml").decode())
    project = config.load(pending.root, text=configured)
    fingerprint = _inputs(project)
    directory = project.build / "setup"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="proof-", dir=directory) as temporary:
        tree = Path(temporary) / "tree"
        _copy_inputs(project, tree, fingerprint)
        atomic_files.text(tree / "config.toml", configured)
        staged = config.load(tree)
        if supply is not None:
            restore_roms(staged, supply)
        manifest = project.build / "setup/roms.json"
        if manifest.is_file():
            destination = staged.build / "setup/roms.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_files.copy2(manifest, destination)
            measured = census.run(config.load_pending(tree), policy, names_from=project.names_from)
            layout_path = tree / "build/setup/layout.json"
            if layout_path.is_file():
                _ready_readme(project, measured, cast("LayoutManifest", json.loads(layout_path.read_bytes())), tree)
            del measured
        return _prove_publish(project, tree, policy, fingerprint, fresh=False, supply=supply)
