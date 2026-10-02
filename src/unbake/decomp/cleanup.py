"""Prepare authored editable C against shared types, without publishing it."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from unbake.decomp import checks, gbi, type_context, work
from unbake.layout import split, split_apply
from unbake.match import declarations
from unbake.match.common import atomic
from unbake.project.config import Held, Policy, Project


def prepare(project: Project, policy: Policy, source: Path) -> Path:
    source = source.resolve()
    if source.suffix != ".c" or not source.is_file() or not source.is_relative_to(project.drafts.resolve()):
        raise Held("cleanup", "cleanup.source: required editable .c file under paths.drafts")
    type_context.required(project)
    versions = split.holding_versions(project, source.stem)
    project.work.mkdir(parents=True, exist_ok=True)
    original = source.read_bytes()
    with tempfile.TemporaryDirectory(prefix=source.stem + ".cleanup.", dir=project.work) as temporary:
        directory = Path(temporary)
        if (source.parent / "overlay.json").exists():
            work.overlay_data(project, source)
            shutil.copytree(source.parent / "overlay", directory / "overlay")
            shutil.copyfile(source.parent / "overlay.json", directory / "overlay.json")
            staged = work.overlay_project(project, directory)
        else:
            staged = work.overlay(project, directory)
        lowered = gbi.prepare(staged, original.decode(), gbi.microcode(staged))
        text = lowered.source
        if "gbi" in lowered.headers:
            text = gbi.install(staged) + text
        if "abi" in lowered.headers:
            text = gbi.install_audio(staged) + text
        edits = declarations.folded_edits(staged, policy, source.stem, text, versions)
        headers = [edit for edit in edits if any(edit.path.is_relative_to(root) for root in staged.include)]
        split_apply._write_staging(staged, headers)
        prepared = next(edit.after for edit in edits if edit.path == staged.src / source.name)
        blockers = [finding for finding in checks.run(prepared) if finding.fakematch is None]
        if blockers:
            raise Held("cleanup", "cleanup.source_rules: " + "; ".join(checks.message(f) for f in blockers))
        work.save_overlay(project, directory)
        if source.read_bytes() != original:
            raise Held("cleanup", "cleanup.source_sha256: editable source changed during preparation")
        shutil.copytree(directory / "overlay", source.parent / "overlay", dirs_exist_ok=True)
        atomic(source.parent / "overlay.json", (directory / "overlay.json").read_bytes())
        atomic(source, prepared.encode())
    return source
