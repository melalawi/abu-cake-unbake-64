"""Bootstrap a proven all-assembly project from cartridge images."""

import itertools
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from unbake.layout import split, split_create, split_partition
from unbake.project import build, config, fingerprint, init_config, init_proof, rom
from unbake.project.config import Held, Policy, Project
from unbake.project.toolchain import CompilerSpec


@dataclass(frozen=True)
class Forced:
    compiler: dict[str, str] = field(default_factory=dict)
    version_names: dict[str, str] = field(default_factory=dict)
    names_from: str | None = None
    name: str | None = None
    title: str | None = None
    split: str | None = None
    supply: Path | None = None


def naming_version(versions: tuple[str, ...], names_from: str | None) -> str:
    """Require the naming VERSION explicitly for a multi-VERSION project."""
    if not versions:
        raise Held("init", "project.versions: missing values")
    if names_from is None:
        if len(versions) != 1:
            raise Held("init", "--names-from: required for more than one VERSION")
        return versions[0]
    if names_from not in versions:
        raise Held("init", f"--names-from {names_from}: unknown VERSION")
    return names_from


def forced_compiler(region: fingerprint.Region, forced: Forced, specs: dict[str, CompilerSpec]) -> str | None:
    keys = [region.name, "*"]
    for key in forced.compiler:
        match = re.fullmatch(r"(0x[\da-fA-F]+)-(0x[\da-fA-F]+)", key)
        if match and (int(match[1], 16), int(match[2], 16)) == (region.start, region.end):
            keys.append(key)
    values = [forced.compiler[key] for key in keys if key in forced.compiler]
    if len(set(values)) > 1:
        raise Held("init", f"--compiler {region.key}: conflicting forced assignments {values}")
    if not values:
        return None
    ident = values[0]
    if ident not in specs:
        raise Held("init", f"--compiler {ident}: unknown compiler id")
    return ident


def forced_ranges(
    regions: list[fingerprint.Region], cartridge: rom.Rom, forced: Forced, specs: dict[str, CompilerSpec]
) -> list[fingerprint.Region]:
    """An explicit range partitions functions before its compiler is assigned."""
    ranges = []
    functions = [function for region in regions for function in region.functions]
    boundaries = {function.address for function in functions}
    boundaries.update(function.address + function.end - function.start for function in functions)
    for key, ident in forced.compiler.items():
        if ident not in specs:
            raise Held("init", f"--compiler {ident}: unknown compiler id")
        match = re.fullmatch(r"(0x[\da-fA-F]+)-(0x[\da-fA-F]+)", key)
        if not match:
            continue
        start, end = int(match[1], 16), int(match[2], 16)
        if start >= end or start not in boundaries or end not in boundaries:
            raise Held("init", f"--compiler {key}: range must end on measured function boundaries")
        ranges.append((start, end, ident))
    ranges.sort()
    if any(left[1] > right[0] for left, right in itertools.pairwise(ranges)):
        raise Held("init", "--compiler: overlapping forced ranges")
    if not ranges:
        return regions
    groups: list[tuple[tuple[int, int, str] | tuple[str, str | None], list[split.Function], str | None]] = []
    for region in regions:
        for function in region.functions:
            address_end = function.address + function.end - function.start
            selected = next(
                ((start, end, ident) for start, end, ident in ranges if start <= function.address < end), None
            )
            if selected and address_end > selected[1]:
                raise Held("init", f"--compiler 0x{selected[0]:X}-0x{selected[1]:X}: cuts function {function.name}")
            group_key: tuple[int, int, str] | tuple[str, str | None] = (
                selected if selected else (region.name, region.family)
            )
            if groups and groups[-1][0] == group_key:
                groups[-1][1].append(function)
            else:
                groups.append((group_key, [function], region.family))
    output: list[fingerprint.Region] = []
    counts_by_name: dict[str, int] = {}
    for group_key, members, family in groups:
        counts = fingerprint.idioms(cartridge, [(function.start, function.end) for function in members])
        total = fingerprint.Counts(
            sum(count.addu for count in counts.values()), sum(count.or_ for count in counts.values())
        )
        named_family = specs[group_key[2]].family if len(group_key) == 3 else family
        base = "main" if named_family == "gcc" else named_family or "undecided"
        counts_by_name[base] = counts_by_name.get(base, 0) + 1
        name = base if counts_by_name[base] == 1 else f"{base}_{members[0].address:08X}"
        start, end = (
            group_key[:2]
            if len(group_key) == 3
            else (members[0].address, members[-1].address + members[-1].end - members[-1].start)
        )
        output.append(
            fingerprint.Region(start, end, total.family if len(group_key) == 3 else family, total, name, tuple(members))
        )
    return output


def _install(project: Project, policy: Policy, supply: Path | None) -> None:
    from unbake.project import toolchain

    if supply is None:
        toolchain.ensure(project, policy)
    else:
        toolchain.ensure(project, policy, supply=supply)


def readme(
    project: Project,
    title: str,
    cartridges: list[rom.Rom],
    names: dict[Path, str],
) -> None:
    from unbake.project.header import DESTINATIONS
    from unbake.report.progress import render
    from unbake.report.units import functions

    descriptions, reports = [], {}
    for cartridge in cartridges:
        version = names[cartridge.path]
        descriptions.append(
            f"| {version} ({DESTINATIONS[cartridge.header.region]}, revision {cartridge.header.revision}) |"
        )
        measured = functions(project.version(version))
        reports[version] = {
            "version": 2,
            "measures": {
                "complete_code": 0,
                "total_code": sum(function.end - function.start for function in measured),
                "complete_units": 0,
                "total_units": len(measured),
            },
        }
    template = (
        f"# {title}\n\nAn N64 decompilation project.\n\n## Progress\n\n"
        + "\n\n".join(descriptions)
        + "\n\n## Building\n\nRun `make VERSION=<version>` to build a cartridge.\n"
        "Run `make check VERSION=<version>` to compare it with the original ROM.\n"
    )
    (project.root / "README.md").write_text(render(template, reports), encoding="utf-8")


def run(target: Path, roms: list[Path], forced: Forced) -> list[str]:
    from unbake.project import setup, toolchain

    if target is None:
        raise Held("init", "target: missing directory")
    if not isinstance(roms, list) or not roms:
        raise Held("init", "roms: required non-empty ROM list")
    if not isinstance(forced, Forced):
        raise Held("init", "forced: required Forced values")
    target = Path(target).expanduser().absolute()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise Held("init", f"target {target}: exists and is non-empty")
    files = [Path(path).expanduser().absolute() for path in roms]
    cartridges = [rom.load(path) for path in files]
    seen: dict[str, Path] = {}
    for cartridge in cartridges:
        if cartridge.sha1 in seen:
            raise Held("init", f"{cartridge.path}: duplicate sha1 {cartridge.sha1} ({seen[cartridge.sha1]})")
        seen[cartridge.sha1] = cartridge.path
    for command in ("git", "make"):
        if shutil.which(command) is None:
            raise Held("init", f"{command}: missing executable")
    policy = config.load_policy()
    if shutil.which(str(policy.splat)) is None:
        raise Held("init", f"policy.splat {policy.splat}: missing executable")
    names = init_config.ordered_versions(rom.version_names(cartridges, forced.version_names), forced.version_names)
    by_path = {cartridge.path: cartridge for cartridge in cartridges}
    cartridges = [by_path[path] for path in names]
    names_from = naming_version(tuple(names.values()), forced.names_from)
    reference = next(cartridge for cartridge in cartridges if names[cartridge.path] == names_from)
    title = forced.title if forced.title is not None else reference.header.title
    if not title or not title.strip():
        raise Held("init", "title: empty ROM header; pass --title")
    name = rom.stem(forced.name if forced.name is not None else re.sub(r"[^a-z0-9]", "", title.lower()), "--name")
    mode = forced.split if forced.split is not None else init_config.required(policy, "init_split")
    if mode not in ("functions", "files"):
        raise Held("init", f"split {mode}: expected functions or files")
    target.parent.mkdir(parents=True, exist_ok=True)
    author = {
        key: init_proof.run(["git", "config", "--get", f"user.{key}"], target.parent) for key in ("name", "email")
    }
    for key, value in author.items():
        if not value:
            raise Held("init", f"git config user.{key}: unset")
    receipts = []

    def receipt(line: str) -> None:
        receipts.append(line)
        print(line)

    work = target.with_name(f"{target.name}.init-{os.getpid()}")
    if work.exists():
        raise Held("init", f"temporary directory {work}: already exists")
    work.mkdir()
    existed = target.exists()
    published = False
    try:
        layouts, inventory = {}, {}
        for dirname in ("src", "include", "tools"):
            (work / dirname).mkdir()
        (work / "include" / "types.h").write_text(
            "typedef signed char s8;\ntypedef unsigned char u8;\ntypedef signed short s16;\n"
            "typedef unsigned short u16;\ntypedef signed int s32;\ntypedef unsigned int u32;\n"
            "typedef signed long long s64;\ntypedef unsigned long long u64;\n"
            "typedef float f32;\ntypedef double f64;\n"
        )
        for cartridge in cartridges:
            version = names[cartridge.path]
            normalised = work / f"baserom.{version}.z64"
            normalised.write_bytes(cartridge.data)
            directory = work / "versions" / version
            directory.mkdir(parents=True)
            (directory / "baserom.sha1").write_text(f"{cartridge.sha1}  {normalised.name}\n")
            (directory / "symbol_addrs.txt").write_text("")
            layout = directory / f"{name}.yaml"
            layout.write_text(split_create.create(normalised, name, version, policy=policy))
            init_proof.run(
                [str(policy.splat), "split", str(layout.relative_to(work))],
                work,
                work / "build" / "init" / f"{version}.split.log",
            )
            measured = split.extracted_text(cast(Project, SimpleNamespace(asm=work / "asm")), version)
            functions = measured.functions
            inventory[version] = functions
            layouts[version] = layout
            if mode == "functions":
                layout.write_text(split_partition.cut_functions(layout.read_text(), functions))
            layout.write_text(split_partition.type_text(layout, measured))
            for clue in fingerprint.evidence(cartridge):
                receipt(f"clue {version}: {clue}")
        matrix = rom.same_game(
            cartridges,
            {cartridge.path: inventory[names[cartridge.path]] for cartridge in cartridges},
            init_config.required(policy, "init_same_game_similarity"),
        )
        for cartridge in cartridges:
            values = " ".join(f"{names[other.path]}={matrix[cartridge, other]:.6f}" for other in cartridges)
            receipt(f"same-game {names[cartridge.path]}: {values}")
        specs = toolchain.registry()
        reference_regions = forced_ranges(
            fingerprint.regions(inventory[names_from], reference), reference, forced, specs
        )
        regional = {names_from: reference_regions}
        for cartridge in cartridges:
            version = names[cartridge.path]
            if version != names_from:
                regional[version] = (
                    fingerprint.regions(inventory[version], cartridge)
                    if "*" in forced.compiler
                    else fingerprint.confirm(reference_regions, reference, inventory[version], cartridge, version)
                )
        assignments: dict[str, str] = {}
        used: set[str] = set()
        for region in reference_regions:
            receipt(
                f"family {region.name} {region.key}: {region.family or 'undecided'} "
                f"addu={region.counts.addu} or={region.counts.or_}"
            )
        for region in reference_regions:
            forced_id = forced_compiler(region, forced, specs)
            if forced_id is not None:
                assignments[region.name] = forced_id
                used.update(
                    key
                    for key in forced.compiler
                    if key in (region.name, "*")
                    or (
                        re.fullmatch(r"(0x[\da-fA-F]+)-(0x[\da-fA-F]+)", key)
                        and tuple(int(value, 16) for value in key.split("-")) == (region.start, region.end)
                    )
                )
                if region.family is not None and specs[forced_id].family != region.family:
                    receipt(
                        f"warning {region.key}: forced {forced_id} family {specs[forced_id].family} "
                        f"contradicts {region.family}"
                    )
                receipt(f"compiler {region.name} {region.key}: {forced_id}; forced")
                continue
            if region.family is None:
                raise Held(
                    "init",
                    f"region {region.key}: family undecided "
                    f"(addu={region.counts.addu}, or={region.counts.or_}); pass --compiler {region.key}=<id>",
                )
            candidates = [spec for spec in specs.values() if spec.family == region.family]
            provisional = {spec.id: spec.id for spec in candidates}
            init_config.write_config(work, name, title, cartridges, names, names_from, provisional, specs, policy)
            project = config.load(work)
            _install(project, policy, forced.supply)
            decision = fingerprint.prove(project, region, candidates, policy)
            scores = ", ".join(
                f"{ident} reproduces {score}/{len(decision.probes)}" for ident, score in decision.scores.items()
            )
            receipt(f"probes {region.name} {region.key}: {scores}; chosen={decision.id or 'undecided'}")
            for probe, error in decision.errors.items():
                receipt(f"probe {region.name} {probe}: {error}")
            if decision.id is None:
                raise Held(
                    "init",
                    f"region {region.key} ({region.family}): release undecided: {scores}; "
                    f"pass --compiler {region.key}=<id>",
                )
            assignments[region.name] = decision.id
        for key in forced.compiler.keys() - used:
            raise Held("init", f"--compiler {key}: unknown region")
        for version, regions in regional.items():
            for region in regions:
                if region.name not in assignments:
                    if "*" in forced.compiler:
                        assignments[region.name] = forced.compiler["*"]
                    else:
                        raise Held("init", f"VERSION {version} region {region.key}: no reference compiler assignment")
            layouts[version].write_text(split_partition.cut_segments(layouts[version].read_text(), regions))
        init_config.write_config(work, name, title, cartridges, names, names_from, assignments, specs, policy)
        project = config.load(work)
        _install(project, policy, forced.supply)
        setup.run(project, policy)
        ignored = ["__pycache__/", "*.py[cod]", "baserom.*", "build/", "asm/"]
        ignored.extend(f"tools/{ident}/" for ident in dict.fromkeys(assignments.values()))
        (work / ".gitignore").write_text("\n".join(ignored) + "\n")
        for cartridge in cartridges:
            version = names[cartridge.path]
            init_proof.proof(work, name, version, cartridge.data, policy.cores)
            build.current_generation(project, version)
            receipt(
                f"OK(init): VERSION {version} region={cartridge.header.region} "
                f"revision={cartridge.header.revision} sha1={cartridge.sha1} byte-identical"
            )
        readme(project, title, cartridges, names)
        if existed:
            target.rmdir()
        work.rename(target)
        published = True
        environment = dict(
            os.environ,
            GIT_AUTHOR_NAME=author["name"],
            GIT_AUTHOR_EMAIL=author["email"],
            GIT_COMMITTER_NAME=author["name"],
            GIT_COMMITTER_EMAIL=author["email"],
        )
        init_proof.run(["git", "init", "-b", "main"], target, environment=environment)
        init_proof.run(
            ["git", "add", "-A", "--", ".", ":(exclude)**/__pycache__/**", ":(exclude)*.pyc", ":(exclude)*.pyo"],
            target,
            environment=environment,
        )
        init_proof.run(
            ["git", "commit", "-m", f"Initial all-asm split of {title} ({', '.join(names.values())})"],
            target,
            environment=environment,
        )
        receipt(f"OK(init): {target.name}: initial all-asm commit")
        return receipts
    except BaseException:
        if published:
            shutil.rmtree(target)
            if existed:
                target.mkdir()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
