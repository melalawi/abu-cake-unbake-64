"""Reviewable compiler evidence and confirmation before setup publication."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unbake.compilers import files as compiler_files
from unbake.compilers import probe as compiler_probes
from unbake.compilers import profiles as compiler_profiles
from unbake.compilers import registry as toolchain
from unbake.config import Held, PendingProject, Host

if TYPE_CHECKING:
    from unbake.project.census import Census
    from unbake.project.flow import CompilerCandidate, CompilerProposal, LayoutManifest


def encoded(value: object) -> bytes:
    def serialize(item: object) -> str:
        if isinstance(item, Path):
            return str(item)
        raise TypeError(f"unsupported proposal input {type(item).__name__}")

    return (json.dumps(value, separators=(",", ":"), sort_keys=True, default=serialize) + "\n").encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def proposal_path(project: PendingProject) -> Path:
    path = project.build / "setup/proposal.json"
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise Held("setup", "setup.compiler_proposal: proposal path contains a symlink")
    return path


def _inputs(
    project: PendingProject,
    layout: LayoutManifest,
    policy: Host,
    choices: dict[str, str],
    *,
    layout_sha256: str | None = None,
) -> dict[str, str]:
    try:
        registry = toolchain.REGISTRY_PATH.read_bytes()
        config = (project.root / "config.toml").read_bytes()
    except OSError as error:
        raise Held("setup", f"setup.compiler_proposal: {error}") from error
    inputs = {
        "policy": digest(asdict(policy)),
        "registry": hashlib.sha256(registry).hexdigest(),
        "profiles": digest(toolchain._read(toolchain.REGISTRY_PATH).get("fingerprints", {})),
        "choices": digest(choices),
        "layout": layout_sha256 if layout_sha256 is not None else digest(layout),
        "pending_config": hashlib.sha256(config).hexdigest(),
        "evidence_engine": digest(
            {
                name: compiler_files.sha(Path(__file__).with_name(name))
                for name in (
                    "compiler_proposal.py",
                    "compiler_profiles.py",
                    "compiler_probes.py",
                    "fingerprint.py",
                    "proposal_accept.py",
                )
            }
        ),
    }
    layout_file = project.build / "setup/layout.json"
    if layout_file.exists():
        inputs["layout_file"] = compiler_files.sha(layout_file)
    return inputs


def _verify_identity(project: PendingProject, census: Census, layout: LayoutManifest) -> dict[str, str]:
    for key in ("schema", "project_id", "rom_sha1", "versions", "names_from", "inputs_sha256"):
        if key not in layout:
            raise Held("setup", f"setup.compiler_proposal: layout.{key}: missing input")
    hashes = {census.names[rom.path]: rom.sha1 for rom in census.cartridges}
    if (
        layout["schema"] != 1
        or layout["project_id"] != project.id
        or layout["rom_sha1"] != hashes
        or set(layout["versions"]) != set(hashes)
        or layout["names_from"] != census.names_from
    ):
        raise Held("setup", "setup.proposal_stale: layout identity, ROM set or naming version differs")
    if not hashes:
        raise Held("setup", "setup.compiler_proposal: no ROMs")
    return hashes


def _choices(project: PendingProject, choices: dict[str, str] | None) -> dict[str, str]:
    if choices is not None:
        return dict(choices)
    path = proposal_path(project)
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_bytes())
        if previous["project_id"] != project.id:
            raise ValueError("proposal belongs to another project")
        saved = previous.get("choices", {})
        if not isinstance(saved, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in saved.items()
        ):
            raise ValueError("invalid compiler choices")
        return saved
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Held("setup", f"setup.compiler_proposal: persisted proposal: {error}") from error


def propose_compilers(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    policy: Host,
    *,
    choices: dict[str, str] | None = None,
) -> CompilerProposal:
    """Persist measured ranks only; no config, compiler install or publication."""
    from unbake.decomp.guide import prologue
    from unbake.compilers.fingerprint import evidence

    hashes = _verify_identity(project, census, layout)
    specs = toolchain.registry()
    profiles, rules = compiler_profiles.read()
    selected = _choices(project, choices)
    for region, ident in selected.items():
        if ident not in specs:
            raise Held("setup", f"setup.compiler_candidate: {region}={ident}: unknown registry ID")
        if ident not in profiles:
            raise Held("setup", f"setup.compiler_candidate: {region}={ident}: unsupported fingerprint profile/ABI")
    # Regions collect measured family evidence across a loaded version. Unit
    # measurements retain every ROM span; no unknown function inherits a family.
    regions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unit_regions: dict[str, list[str]] = defaultdict(list)
    unit_measurements: dict[str, list[dict[str, Any]]] = defaultdict(list)
    clues = {}
    for rom in census.cartridges:
        version = census.names[rom.path]
        if "functions" not in layout["versions"][version]:
            raise Held("setup", f"setup.compiler_proposal: layout.versions.{version}.functions: missing input")
        functions = layout["versions"][version]["functions"]
        if not isinstance(functions, list):
            raise Held("setup", f"setup.compiler_proposal: layout.versions.{version}.functions: expected array")
        if not functions:
            raise Held("setup", f"setup.compiler_proposal: {version}: layout functions missing")
        for index, function in enumerate(functions):
            for key in ("start", "end", "address", "name"):
                if key not in function:
                    raise Held(
                        "setup",
                        f"setup.compiler_proposal: layout.versions.{version}.functions.{index}.{key}: missing input",
                    )
            if type(function["start"]) is not int:
                raise Held("setup", f"setup.compiler_proposal: {version}: functions.{index}.start: expected integer")
        image = rom.image()
        clues[version] = evidence(rom)
        previous_end = -1
        names = set()
        for function in sorted(functions, key=lambda item: item["start"]):
            start, end, address, name = (function["start"], function["end"], function["address"], function["name"])
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(image)
                or start % 4
                or end % 4
                or start < previous_end
                or type(address) is not int
                or not isinstance(name, str)
                or not name
                or name in names
            ):
                raise Held("setup", f"setup.compiler_proposal: {version}:{name}: invalid/overlapping function span")
            names.add(name)
            previous_end = end
            body = image[start:end]
            words = tuple(word for (word,) in struct.iter_unpack(">I", body))
            measured = compiler_profiles.measure(body)
            moves = measured["addu_moves"] + measured["or_moves"]
            family = "undecided"
            if moves >= rules["minimum_moves"]:
                numerator, denominator = rules["agreement_numerator"], rules["agreement_denominator"]
                if measured["addu_moves"] * denominator >= moves * numerator:
                    family = "gcc"
                elif measured["or_moves"] * denominator >= moves * numerator:
                    family = "ido"
                else:
                    family = "mixed"
            region = f"{version}:{family}"
            matches = {
                ident: [example.name for example in profile.exemplars if example.matches(words)]
                for ident, profile in profiles.items()
            }
            regions[region].append(
                {
                    "name": name,
                    "start": start,
                    "end": end,
                    "address": address,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "prologue": prologue(words),
                    "prologue_words": [
                        {"rom_offset": start + index * 4, "word": f"0x{word:08X}"}
                        for index, word in enumerate(words[:16])
                    ],
                    "features": measured,
                    "exemplar_matches": matches,
                    "ranks": compiler_profiles.rank(measured, matches, profiles),
                }
            )
            unit_regions[name].append(region)
            unit_measurements[name].append(regions[region][-1])
        del image
    unknown = set(selected) - (set(regions) | set(unit_regions) | {"default"})
    if unknown:
        key = "setup.proposal_stale" if choices is None else "setup.compiler_candidate"
        raise Held("setup", f"{key}: unknown region/unit: {', '.join(sorted(unknown))}")
    available = {}
    availability = {}
    for ident, spec in specs.items():
        try:
            toolchain.verify(policy.cache_root / "compilers" / ident, spec)
        except Held as error:
            available[ident] = False
            availability[ident] = error.reason
        else:
            available[ident] = True
            availability[ident] = "installed; pins verified"
    candidates: dict[str, list[CompilerCandidate]] = {}
    winners: dict[str, str | None] = {}
    reasons: dict[str, str] = {}
    first_try: dict[str, list[str]] = {}
    probes: dict[str, Any] = {}
    cartridges = {census.names[rom.path]: rom for rom in census.cartridges}
    for region, units in sorted(regions.items()):
        measured = {feature: sum(unit["features"][feature] for unit in units) for feature in compiler_profiles.FEATURES}
        matches = {
            ident: [unit["name"] + "/" + name for unit in units for name in unit["exemplar_matches"].get(ident, [])]
            for ident in profiles
        }
        ranks = compiler_profiles.rank(measured, matches, profiles)
        rows: list[CompilerCandidate] = [
            {
                "id": ident,
                "available": available[ident],
                "rank": ranks.get(ident, [0, 0, 0, 0]),
                "evidence": {
                    "supported": ident in profiles,
                    "abi": profiles[ident].abi if ident in profiles else None,
                    "family": spec.family,
                    "features": measured,
                    "exemplar_matches": matches.get(ident, []),
                    "comparable_exemplars": len(profiles[ident].exemplars) if ident in profiles else 0,
                    "availability": availability[ident],
                    "compiler_pins": spec.pins,
                    "cflags": list(spec.cflags),
                },
            }
            for ident, spec in specs.items()
        ]
        rows.sort(key=lambda row: (tuple(-value for value in row["rank"]), row["id"]))
        candidates[region] = rows
        supported = [row for row in rows if row["evidence"]["supported"]]
        best = supported[0]["rank"] if supported else [0, 0, 0, 0]
        tied = [row["id"] for row in supported if row["rank"] == best]
        if region.endswith(":undecided"):
            # Weak move evidence cannot decide a family by aggregate rank.
            tied = sorted(profiles)
        if len(tied) > 1 and region not in selected and not region.endswith(":undecided"):
            probe = compiler_probes.reproduce(project, policy, tied, units, cartridges[region.split(":")[0]].image())
            probes[region] = probe
            scores = {ident: row["score"] for ident, row in probe["candidates"].items()}
            probe_best = max(scores.values())
            if not probe["errors"]:
                tied = [ident for ident in tied if scores[ident] == probe_best]
            probe["decision"] = "incomplete probes; retain tied set" if probe["errors"] else "complete comparisons"
            for row in rows:
                if row["id"] in scores:
                    row["evidence"]["reproduction"] = probe["candidates"][row["id"]]
        # The regional rank is the displayed proposal. A clear winner is
        # accepted as part of the whole digest.
        mixed = region.endswith(":mixed")
        choice = selected.get(region)
        if choice:
            winners[region] = choice
        elif len(tied) == 1 and (
            any(best[:3]) or any(probes.get(region, {}).get("candidates", {}).get(tied[0], {}).get("score", []))
        ):
            winners[region] = tied[0]
        elif len(tied) > 1 and not mixed and not region.endswith(":undecided"):
            # Bytes cannot separate these candidates at region level. The first
            # tied member in registry order is compiled first; try ranks the rest.
            first_try[region] = [ident for ident in specs if ident in tied]
            winners[region] = first_try[region][0]
        else:
            winners[region] = None
            reasons[region] = "mixed" if mixed else "tie" if len(tied) > 1 else "no evidence"
    from unbake.compilers.accept import assignments as accept_assignments

    assignments, unresolved, default = accept_assignments(unit_regions, winners, selected)
    # Weak or contradictory regional evidence never pins a unit: it builds with
    # the default first, and try measures every other compiler when not exact.
    weak = [
        name
        for name, containing in unit_regions.items()
        if name not in selected
        and (
            any(region.endswith((":undecided", ":mixed")) for region in containing)
            or len({winners[region] for region in containing}) > 1
        )
    ]
    unresolved = [
        reason
        for reason in unresolved
        if not (reason.startswith("unit:") and reason.split(":")[1] in weak)
        and not (reason in regions and all(row["name"] in weak for row in regions[reason]))
    ]
    if default is not None:
        for name in weak:
            assignments[name] = default
    used = set(assignments.values()) | ({default} if default else set())
    document: dict[str, Any] = {
        "schema": 1,
        "project_id": project.id,
        "rom_sha1": hashes,
        "layout_sha256": digest(layout),
        "inputs_sha256": _inputs(project, layout, policy, selected),
        "default_compiler": default,
        "assignments": assignments,
        "cflags": {ident: list(specs[ident].cflags) for ident in sorted(used)},
        "candidates": candidates,
        "unresolved": sorted(set(unresolved)),
    }
    # Additive evidence retains units and choices without changing the contract.
    document.update(
        choices=selected,
        regions=dict(regions),
        region_choices=winners,
        unresolved_reasons=reasons,
        clues=clues,
        ranking_policy=rules,
        first_try=first_try,
        default_units=sorted(weak),
        source_reproduction_probes={
            "attempted": sum(p["attempted"] for p in probes.values()),
            "successful_comparable": sum(p["successful_comparable"] for p in probes.values()),
            "errors": [error for p in probes.values() for error in p["errors"]],
            "regions": probes,
        },
    )
    compiler_files.atomic_bytes(proposal_path(project), encoded(document))
    return cast("CompilerProposal", document)


def receipt(proposal: CompilerProposal) -> list[str]:
    lines = []
    winners = cast(dict[str, str | None], proposal.get("region_choices", {}))
    for region, candidates in proposal["candidates"].items():
        lines.append(f"compiler region {region}: selection={winners.get(region) or 'unresolved'}")
        for candidate in candidates:
            features = candidate["evidence"]["features"]
            measurements = " ".join(f"{key}={value}" for key, value in sorted(features.items()) if value)
            lines.append(
                f"  {candidate['id']}: rank={candidate['rank']} available={candidate['available']} "
                f"supported={candidate['evidence']['supported']} {measurements} "
                f"exemplars={len(candidate['evidence']['exemplar_matches'])}"
            )
    lines.append(f"compiler default: {proposal['default_compiler'] or 'unresolved'}")
    counts: dict[str, int] = defaultdict(int)
    for ident in proposal["assignments"].values():
        counts[ident] += 1
    for region, ids in cast(dict[str, list[str]], proposal.get("first_try", {})).items():
        lines.append(f"compiler region {region}: bytes tie {{{', '.join(ids)}}}; {ids[0]} compiles first")
    weak = cast(list[str], proposal.get("default_units", []))
    if weak:
        lines.append(f"compiler default first: {len(weak)} units with weak evidence; try ranks others when not exact")
    for ident, count in sorted(counts.items()):
        lines.append(f"compiler {ident}: {count} unit assignments; flags={' '.join(proposal['cflags'][ident])}")
    lines.append("exact per-unit assignments and prologue/codegen evidence: build/setup/proposal.json")
    if proposal["unresolved"]:
        lines.append("unresolved compiler choices: " + ", ".join(proposal["unresolved"]))
        lines.append("supply setup --compiler REGION=ID (or UNIT=ID) for the named unresolved choices")
    else:
        lines.append(f"reviewed proposal: setup --confirm {hashlib.sha256(encoded(proposal)).hexdigest()}")
    return lines


def confirm_proposal(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    proposal: CompilerProposal,
    policy: Host,
    *,
    confirm: str | None = None,
) -> None:
    """Require the exact current proposal before the caller writes game facts."""
    path = proposal_path(project)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise Held("setup", f"setup.proposal_stale: persisted proposal missing: {error}") from error
    token = hashlib.sha256(content).hexdigest()
    choices = proposal.get("choices", {})
    current = _inputs(project, layout, policy, choices)
    if (
        content != encoded(proposal)
        or proposal["inputs_sha256"] != current
        or proposal["rom_sha1"] != _verify_identity(project, census, layout)
        or proposal["layout_sha256"] != digest(layout)
    ):
        raise Held(
            "setup", "setup.proposal_stale: proposal bytes or ROM/policy/registry/profile/layout/choices changed"
        )
    for rom in census.cartridges:
        version = census.names[rom.path]
        for source in {rom.path, project.roms / f"baserom.{version}.z64"}:
            try:
                actual = hashlib.sha1(source.read_bytes()).hexdigest()
            except OSError as error:
                raise Held("setup", f"setup.proposal_stale: {version}: {error}") from error
            # Originals may use v64/n64 byte order; census.data is normalized.
            if source == rom.path and actual != rom.sha1:
                from unbake.project.rom import normalise

                try:
                    actual = hashlib.sha1(normalise(source.read_bytes())).hexdigest()
                except Held as error:
                    raise Held("setup", f"setup.proposal_stale: ROM {version}: {error.reason}") from error
            if actual != proposal["rom_sha1"][version]:
                raise Held("setup", f"setup.proposal_stale: ROM {version}: sha1 changed")
    if confirm is not None and confirm != token:
        raise Held("setup", f"setup.proposal_stale: confirmation digest differs; review setup --confirm {token}")
    if proposal["unresolved"]:
        mixed = any("mixed" in value for value in proposal["unresolved"])
        key = "setup.compiler_mixed" if mixed else "setup.compiler_candidate"
        raise Held("setup", f"{key}: unresolved/tied choices: {', '.join(proposal['unresolved'])}")
    if not proposal["default_compiler"] or not proposal["assignments"]:
        raise Held("setup", "setup.compiler_candidate: missing default compiler or unit assignments")
    if confirm is None:
        if not sys.stdin.isatty():
            raise Held("setup", f"setup.compiler_confirmation: review and run setup --confirm {token}")
        try:
            answer = input(f"Accept displayed compiler assignments and flags ({token})? [yes/no]: ").strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise Held("setup", "setup.compiler_confirmation: EOF or interrupted acceptance") from error
        if answer.lower() != "yes":
            raise Held("setup", "setup.compiler_confirmation: rejected; explicit yes required")
        # A file edited while the TTY prompt is open invalidates acceptance.
        confirm_proposal(project, census, layout, proposal, policy, confirm=token)


def confirmation_guard(project: PendingProject, proposal: CompilerProposal, policy: Host) -> Callable[[], None]:
    """Retain input pins, not measured bodies, while the staged cartridges build."""
    token = compiler_files.sha(proposal_path(project))
    expected = dict(proposal["inputs_sha256"])
    layout_sha256 = proposal["layout_sha256"]
    choices = dict(proposal.get("choices", {}))

    def verify() -> None:
        try:
            current = _inputs(project, cast("LayoutManifest", {}), policy, choices, layout_sha256=layout_sha256)
            actual = compiler_files.sha(proposal_path(project))
        except OSError as error:
            raise Held("setup", f"setup.proposal_stale: confirmed input unavailable: {error}") from error
        if actual != token or current != expected:
            raise Held("setup", "setup.proposal_stale: confirmed proposal or input pins changed during proof")

    return verify
