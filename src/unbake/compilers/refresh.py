"""Reconsider compiler evidence on a ready layout without changing its boundaries."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import cast

import toml  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import config
from unbake.compilers import files as compiler_files
from unbake.compilers import propose as compiler_proposal
from unbake.config import Held, Host, PendingProject
from unbake.project import census, setup, setup_config
from unbake.project.flow import LayoutManifest


def run(pending: PendingProject, policy: Host, confirm: str | None) -> list[str]:
    project = config.load(pending.root)
    layout_path = project.build / "setup/layout.json"
    try:
        layout = cast(LayoutManifest, json.loads(layout_path.read_bytes()))
    except (OSError, ValueError) as error:
        raise Held("setup", f"setup.compiler_layout: {error}") from error
    measured = census.run(pending, policy, names_from=project.names_from)
    # Keep published C and measured exception units on their proved recipe.
    retained = {source.stem: project.compiler_reference(source) for source in project.src.rglob("*.c")}
    retained.update(project.units)
    proposal = compiler_proposal.propose_compilers(pending, measured, layout, policy, choices=retained)
    proposal["retained_assignments"] = {  # type: ignore[typeddict-unknown-key]
        "rule": "preserve published C and measured exception units on their proved compiler",
        "assignments": retained,
        "sources": {str(p.relative_to(project.root)): compiler_files.sha(p) for p in project.src.rglob("*.c")},
    }
    compiler_files.atomic_bytes(compiler_proposal.proposal_path(pending), compiler_proposal.encoded(proposal))
    for line in compiler_proposal.receipt(proposal):
        print("OK(setup): " + line.replace("setup --confirm", "setup --repropose-compilers --confirm"))
    compiler_proposal.confirm_proposal(pending, measured, layout, proposal, policy, confirm=confirm)
    fingerprint = setup._inputs(project)
    original = (project.root / "config.toml").read_bytes()
    data = toml.loads(original.decode())
    default = proposal["default_compiler"]
    if default is None:
        raise Held("setup", "setup.compiler_candidate: explicit default compiler required")
    data["project"]["default_compiler"] = default
    data.pop("units", None)
    units = setup_config.exception_units(default, proposal["assignments"])
    if units:
        data["units"] = units
    data["compilers"] = {ident: {"cflags": flags} for ident, flags in proposal["cflags"].items()}
    with tempfile.TemporaryDirectory(prefix="compiler-refresh-", dir=project.build / "setup") as temporary:
        tree = Path(temporary) / "tree"
        setup._copy_inputs(project, tree, fingerprint)
        atomic_files.text(tree / "config.toml", toml.dumps(data))
        staged = config.load(tree)
        setup.run(staged, policy)
        outputs = {
            project.root / relative: (tree / relative).read_bytes()
            for relative in setup._inputs(staged)
            if relative.startswith(project.tools.relative_to(project.root).as_posix() + "/")
        }
        accepted = compiler_proposal.encoded(proposal)
        outputs[project.build / "setup/compiler.json"] = accepted
        outputs[project.build / "setup/confirmation.json"] = (
            json.dumps({"proposal_sha256": hashlib.sha256(accepted).hexdigest()}) + "\n"
        ).encode()
        outputs[project.root / "config.toml"] = (tree / "config.toml").read_bytes()
        from unbake.typemap.mapping import compiler_inputs

        if setup._inputs(project) != fingerprint:
            raise Held("setup", "setup.proposal_stale: project inputs changed during compiler refresh")
        mapped = compiler_inputs(project, outputs[project.root / "config.toml"])
        if mapped is not None:
            outputs[mapped[0]] = mapped[1]
        before = {path: path.read_bytes() if path.is_file() else None for path in outputs}
        try:
            for path, content in outputs.items():
                compiler_files.atomic_bytes(path, content)
                if path.is_relative_to(project.tools):
                    path.chmod((tree / path.relative_to(project.root)).stat().st_mode & 0o777)
        except BaseException:
            for path, old_content in before.items():
                if old_content is None:
                    path.unlink(missing_ok=True)
                else:
                    compiler_files.atomic_bytes(path, old_content)
            raise
    return ["compiler proposal accepted; recipes updated; layout and map retained", "run unbake check"]
