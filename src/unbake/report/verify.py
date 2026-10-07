"""ROM-free deterministic source/report validation and artifact provenance.

Run with python -m unbake.report.verify --project DIR [--artifacts OUT].
The generated CI bundle pins this exporter and its dependencies by content.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from importlib import metadata
from pathlib import Path
from typing import Any

from unbake import atomic, config, strict_json
from unbake.config import Held, Project
from unbake.process import named as cause_named
from unbake.process import temporary_environment
from unbake.report import data, progress, state

BUNDLE = "tools/report-verifier.zip"
MANIFEST = "report-state.json"


def bundle() -> bytes:
    """Deterministic owning-tool payload; archive paths/times never depend on a checkout."""
    import pycparser  # type: ignore[import-untyped]

    root = Path(__file__).parents[1]
    packages = [(root, "unbake"), (Path(pycparser.__file__).parent, "pycparser")]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory, prefix in packages:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix in (".py", ".toml", ".md"):
                    member = zipfile.ZipInfo(f"{prefix}/{path.relative_to(directory).as_posix()}")
                    member.compress_type = zipfile.ZIP_DEFLATED
                    member.external_attr = 0o644 << 16
                    archive.writestr(member, path.read_bytes())
        distribution = metadata.distribution("pycparser")
        license_files = [path for path in distribution.files or () if path.name == "LICENSE"]
        if len(license_files) != 1:
            raise Held(
                cause_named(
                    "report.bundle",
                    "report.bundle: source parser distribution license missing",
                    owner="report.verify",
                    stage="report",
                )
            )
        for name, content in (
            ("LICENSE", (root / "LICENSE").read_bytes()),
            ("pycparser/LICENSE", Path(str(distribution.locate_file(license_files[0]))).read_bytes()),
        ):
            member = zipfile.ZipInfo(name)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o644 << 16
            archive.writestr(member, content)
    return stream.getvalue()


def source_paths(project: Project) -> tuple[Path, ...]:
    """All owning report inputs, shared by pins and transient inventory validation."""
    paths = {
        project.root / "config.toml",
        project.root / BUNDLE,
        project.root / "Makefile",
        project.root / "units.mk",
        project.root / ".github/workflows/progress.yml",
        project.root / ".gitlab-ci.yml",
        project.tools / "compilers.sha256",
    }
    paths.update(path for path in project.src.rglob("*") if path.is_file())
    paths.update(path for root in project.include for path in root.rglob("*") if path.is_file())
    for version in project.versions:
        meta = project.version(version)
        paths.update(
            (
                meta.split,
                meta.symbols,
                project.root / "layout.toml",
                project.tools / "n64link.version",
                project.root / "versions" / version / "symbols.ld",
                project.root / "versions" / version / f"{project.name}.ld",
            )
        )
    paths.add(project.root / "unbake-original-asm.json")
    return tuple(sorted(paths))


def source_pins(project: Project, *, receipts: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    from unbake import cache, inputs
    from unbake.work.attempts import encoded, ledger

    return {
        **{
            path.relative_to(project.root).as_posix(): inputs.digest(path, algorithm="sha256", reuse=cache.configured())
            for path in source_paths(project)
            if path.is_file()
        },
        "native-data-proofs": data.identity(data.snapshots(project)),
        "publication-state": inputs.bytes_digest(
            encoded(ledger(project).fuzzy_sources() if receipts is None else receipts), algorithm="sha256"
        ),
    }


def document(project: Project, current: state.Inventory, reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    versions = {}
    for version, report in reports.items():
        drafts = []
        for row in current.units[version]:
            from unbake.layout import split

            for member in split.unit_members(row):
                name = next((alias for alias in member.aliases if alias in current.receipts), None)
                if name is not None:
                    receipt = current.receipts[name]
                    drafts.append(
                        {
                            "name": member.name,
                            "source_path": f"src/{name}.c",
                            "bytes": member.end - member.start,
                            "score": receipt["versions"][version],
                            "source_sha256": receipt["source_sha256"],
                            "compiler": receipt["compiler"],
                        }
                    )
        known = sum(row["bytes"] * row["score"] / 100 for row in drafts if row["score"] is not None)
        unknown = sum(row["bytes"] for row in drafts if row["score"] is None)
        versions[version] = {
            "report_sha256": hashlib.sha256((json.dumps(report, indent=2) + "\n").encode()).hexdigest(),
            "artifact": f"{version}_report",
            "report_file": f"{version}_report.json",
            "draft_bytes": sum(row["bytes"] for row in drafts),
            "draft_functions": len(drafts),
            "draft_weighted_bytes": known,
            "unknown_similarity_bytes": unknown,
            "drafts": drafts,
            "exact_plus_draft_percent": 100
            * (report["measures"]["matched_code"] + sum(r["bytes"] for r in drafts))
            / report["measures"]["total_code"]
            if report["measures"]["total_code"]
            else 0,
            "data_match_state": current.data_coverage[version].manifest["state"],
            "data_coverage": current.data_coverage[version].manifest,
            "opaque_rom_bytes": max(
                0,
                current.data_coverage[version].manifest["rom_bytes"]
                - report["measures"]["total_code"]
                - report["measures"]["total_data"],
            )
            if current.data_coverage[version].manifest["rom_bytes"] is not None
            else None,
        }
    payload = project.root / BUNDLE
    if not payload.is_file():
        raise Held(
            cause_named(
                "report.tool",
                f"report.tool: {BUNDLE} missing; regenerate owning build files",
                owner="report.verify",
                stage="report",
            )
        )
    pins = source_pins(project, receipts=current.receipts)
    return {
        "schema": 1,
        "tool_sha256": pins[BUNDLE],
        "source_pins": pins,
        "versions": versions,
        "scope": "Declared split code and data intervals only; opaque top-level binary assets excluded. "
        "Data credit requires current source-owned final linked extent proof; unmeasured coverage is explicit. "
        "BSS is outside ROM-data coverage. Fuzzy scalar is the known lower bound; "
        "unknown similarity remains null in drafts. Coverage is independent of similarity.",
    }


def publish_branch(project: Project) -> str:
    workflow = (project.root / ".github/workflows/progress.yml").read_text()
    branches: list[str] = re.findall(r"^    branches: \['((?:[^']|'')+)'\]$", workflow, re.M)
    if len(branches) != 1:
        raise Held(
            cause_named(
                "report.ci",
                "report.ci: exactly one owning publication branch required",
                owner="report.verify",
                stage="report",
            )
        )
    return branches[0].replace("''", "'")


def validate(project: Project) -> dict[str, Any]:
    from unbake import buildfiles

    current = state.inventory(project)
    payload = (project.root / BUNDLE).read_bytes()
    github = buildfiles.github_progress(project, None, publish_branch=publish_branch(project), verifier_payload=payload)
    gitlab = buildfiles.gitlab_progress(project, verifier_payload=payload)
    for ci_path, ci_text in ((".github/workflows/progress.yml", github), (".gitlab-ci.yml", gitlab)):
        if (project.root / ci_path).read_text() != ci_text:
            raise Held(
                cause_named(
                    "report.ci",
                    f"report.ci: {ci_path}: verifier, pins or one-to-one version artifact mapping differs",
                    owner="report.verify",
                    stage="report",
                )
            )
    reports = {v: progress.measure(project, None, v, current=current) for v in project.versions}
    # Every saved report must match complete inventories, categories, measures and exact schema fields.
    for version, expected in reports.items():
        path = project.root / "versions" / version / "report.json"
        if strict_json.read(path) != expected:
            raise Held(
                cause_named(
                    "report.semantic",
                    f"report.semantic: VERSION {version}: source/exporter inventory differs",
                    owner="report.verify",
                    stage="report",
                )
            )
        if path.read_bytes() != (json.dumps(expected, indent=2) + "\n").encode():
            raise Held(
                cause_named(
                    "report.determinism",
                    f"report.determinism: VERSION {version}: serialization differs",
                    owner="report.verify",
                    stage="report",
                )
            )
    expected_state = document(project, current, reports)
    if strict_json.read(project.root / MANIFEST) != expected_state:
        raise Held(
            cause_named(
                "report.state",
                "report.state: source pins, draft state, artifact mapping or tool provenance differs",
                owner="report.verify",
                stage="report",
            )
        )
    template = (project.root / "README.md").read_text()
    if progress.render(template, reports, descriptions=progress.owner_descriptions(project, template)) != template:
        raise Held(
            cause_named(
                "report.readme", "report.readme: generated progress differs", owner="report.verify", stage="report"
            )
        )
    state.assert_current(project, current)
    return expected_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--regenerate", action="store_true", help="Regenerate source-only reports using existing owner README labels."
    )
    args = parser.parse_args()
    try:
        project = config.load(args.project.resolve())
        if args.regenerate:
            if args.artifacts is not None:
                raise Held(
                    cause_named(
                        "report.mode",
                        "report.mode: regeneration and clean artifact validation are separate operations",
                        owner="report.verify",
                        stage="report",
                    )
                )
            from unbake import buildfiles

            buildfiles.write_progress(project, publish_branch=publish_branch(project))
            progress.write(project, None, source_only=True)
        manifest = validate(project)
        if args.artifacts is not None:
            # The caller copies reports only after this succeeds. Hashes bind each report to this checkout.
            sha = os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA")
            if sha is None:
                sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project.root,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=temporary_environment(project.build),
                ).stdout.strip()
                if subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=project.root,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=temporary_environment(project.build),
                ).stdout:
                    raise Held(
                        cause_named(
                            "report.checkout",
                            "report.checkout: tracked checkout must be clean before artifact publication",
                            owner="report.verify",
                            stage="report",
                        )
                    )
            if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
                raise Held(
                    cause_named(
                        "report.commit",
                        "report.commit: immutable 40-hex source revision required",
                        owner="report.verify",
                        stage="report",
                    )
                )
            args.artifacts.mkdir(parents=True, exist_ok=True)
            for version, values in manifest["versions"].items():
                receipt = {"source_commit": sha, "tool_sha256": manifest["tool_sha256"], "version": version, **values}
                atomic.text(args.artifacts / f"{version}_report.manifest.json", json.dumps(receipt, indent=2) + "\n")
        return 0
    except (Held, OSError, ValueError) as error:
        parser.exit(1, f"report verification failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
