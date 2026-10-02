"""Reconsider compiler evidence on a ready layout without changing its boundaries."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import cast

import toml  # type: ignore[import-untyped]

from unbake.project import build, census, compiler_files, compiler_proposal, config, setup
from unbake.project.config import Held, PendingProject, SetupPolicy
from unbake.project.flow import LayoutManifest


def run(pending: PendingProject, policy: SetupPolicy, confirm: str | None) -> list[str]:
    project = config.load(pending.root)
    layout_path = project.root / "docs/setup/layout.json"
    try:
        layout = cast(LayoutManifest, json.loads(layout_path.read_bytes()))
    except (OSError, ValueError) as error:
        raise Held("setup", f"setup.compiler_layout: {error}") from error
    measured = census.run(pending, policy, names_from=project.names_from)
    # Keep published C and byte-backed prior pins on their proved recipe.
    retained = {source.stem: project.compiler_reference(source) for source in project.src.rglob("*.c")}
    selected_functions = {row.get("function") for row in _selections(project)}
    for name, ident in project.units.items():
        if name in selected_functions:
            retained[name] = ident
    if any(ident in project.compiler_ties for ident in retained.values()):
        raise Held("setup", "setup.compiler_retained: published C must have a decided recipe")
    proposal = compiler_proposal.propose_compilers(pending, measured, layout, policy, choices=retained)
    proposal["retained_assignments"] = {  # type: ignore[typeddict-unknown-key]
        "rule": "preserve published C and prior byte-backed pins on their proved compiler",
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
    data["units"] = proposal["assignments"]
    data["compiler_ties"] = proposal.get("compiler_ties", {})
    data["project"]["default_compiler"] = proposal["default_compiler"]
    data["compilers"] = {ident: {"cflags": flags} for ident, flags in proposal["cflags"].items()}
    with tempfile.TemporaryDirectory(prefix="compiler-refresh-", dir=project.build / "setup") as temporary:
        tree = Path(temporary) / "tree"
        setup._copy_inputs(project, tree, fingerprint)
        (tree / "config.toml").write_text(toml.dumps(data))
        staged = config.load(tree)
        setup.run(staged, policy)
        outputs = {
            project.root / relative: (tree / relative).read_bytes()
            for relative in setup._inputs(staged)
            if relative.startswith(project.tools.relative_to(project.root).as_posix() + "/")
        }
        accepted = compiler_proposal.encoded(proposal)
        outputs[project.root / "docs/setup/compiler.json"] = accepted
        outputs[project.root / "docs/setup/confirmation.json"] = (
            json.dumps({"proposal_sha256": hashlib.sha256(accepted).hexdigest()}) + "\n"
        ).encode()
        outputs[project.root / "config.toml"] = (tree / "config.toml").read_bytes()
        from unbake.typemap.mapping import compiler_inputs

        with build.lock(project):
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
    return ["compiler proposal accepted; recipes updated; layout and map retained", "run unbake solve"]


def _selections(project: config.Project) -> list[dict[str, object]]:
    data = toml.loads((project.root / "config.toml").read_text())
    return [json.loads(row["evidence_json"]) for row in data.get("compiler_selections", {}).values()]
