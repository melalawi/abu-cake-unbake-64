"""Prove source edits over a retained extraction without rebuilding unrelated units."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections.abc import Set
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from unbake.layout import split
from unbake.match import forked, reporting, staging
from unbake.fold.common import held
from unbake.project import build, makefile
from unbake.config import Host, Project
from unbake import atomic as atomic_files
from unbake.project_tools import extract, layout
from unbake.project_tools.codegen import dependency_paths
from unbake.objects.elf import Object
from unbake.typemap.storage import file_digest


def _miss(reason: str) -> bool:
    reporting.record("incremental_fallback", reason=reason)
    reporting.learn("OK(submit): incremental fallback: " + reason)
    return False


def advance(project: Project, version: str, generation: Path, before: str, after: str) -> bool:
    """Retarget an extraction when only existing text rows switch from assembly to C."""
    from unbake.match.relink import retarget_rows

    if not retarget_rows(project, generation, before, after):
        return _miss("split spans or retained extraction changed")
    symbols = extract.symbols_from([project.version(version).symbols])
    definitions = generation / "committed_symbols.ld"
    addresses = generation / "symbol-addresses.txt"
    if not definitions.is_file() or not addresses.is_file():
        return _miss("symbol bindings missing")
    bindings = definitions.read_text()
    known = dict(re.findall(r"PROVIDE\((\S+) = (0x[\dA-Fa-f]+)\);", bindings))
    additions = {name: address for name, address in symbols.items() if name not in known}
    if any(int(known[name], 0) != address for name, address in symbols.items() if name in known):
        return _miss("symbol addresses changed")
    for name, address in sorted(additions.items()):
        bindings += f"PROVIDE({name} = 0x{address:08X});\n"
    atomic_files.text(definitions, bindings)
    # Retained generations may contain proved bindings absent from the address
    # inventory. Attribution and assembly must see the same names as the linker.
    inventory = {line.split()[0]: int(line.split()[1], 0) for line in addresses.read_text().splitlines()}
    missing = {name: int(value, 0) for name, value in known.items() if name not in inventory}
    missing.update(additions)
    if missing:
        with atomic_files.stream(addresses, "a") as output:
            output.writelines(f"{name} 0x{address:08X}\n" for name, address in sorted(missing.items()))
    if additions:
        for filename in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
            automatic = generation / filename
            atomic_files.text(automatic, extract.automatic_symbols(automatic.read_text(), additions))
    return True


def changed_sources(original: Project, staged: Project, generation: Path, version: str) -> list[Path]:
    """Use each retained object's header dependencies instead of invalidating all units."""
    sources = []
    digests: dict[Path, str] = {}
    statuses: dict[Path, tuple[bool, int]] = {}

    def status(path: Path) -> tuple[bool, int]:
        if path not in statuses:
            try:
                value = path.stat()
            except OSError:
                statuses[path] = False, 0
            else:
                statuses[path] = stat.S_ISREG(value.st_mode), value.st_mtime_ns
        return statuses[path]

    for name in extract.unit_ranges(staged.version(version).split.read_text()):
        source = staged.src / f"{name}.c"
        previous = original.src / source.name
        obj = generation / "obj/src" / f"{name}.o"
        receipt, dependencies = obj.with_suffix(".built"), obj.with_suffix(".d")
        dirty = not obj.is_file() or not receipt.is_file() or not dependencies.is_file()
        if not dirty:
            stamp = receipt.stat().st_mtime_ns
            evidence = obj.with_suffix(".inputs.json")
            saved = json.loads(evidence.read_text()) if evidence.is_file() else {}
            dirty = not previous.is_file() or file_digest(source) != file_digest(previous)
            words = dependency_paths(dependencies.read_text())
            for word in words:
                path = Path(word)
                if path.is_absolute():
                    if not path.is_relative_to(original.root):
                        dirty = True
                        break
                    path = path.relative_to(original.root)
                local = staged.root / path
                exists, modified = status(local)
                if not exists:
                    dirty = True
                    break
                if str(path) in saved:
                    if local not in digests:
                        digests[local] = hashlib.sha256(local.read_bytes()).hexdigest()
                    if digests[local] != saved[str(path)]:
                        dirty = True
                        break
                elif modified > stamp:
                    dirty = True
                    break
            dirty |= original.compiler_for(previous).id != staged.compiler_for(source).id
        if dirty:
            sources.append(source)
    return sources


def reusable_sources(project: Project, generations: dict[str, Path], sources: dict[str, tuple[str, ...]]) -> set[str]:
    """Recognize objects already compiled from these exact source/dependency bytes.

    Require the saved recipe and receipt age as well as every dependency digest.
    A missing source digest, changed flags, changed header, or old recipe receipt
    takes the ordinary compile path. Cache-wrapper refresh is not a compiler input:
    the historical object already certifies these exact dependency bytes with the
    verified compiler and unchanged flags. Object and cartridge comparisons still run.
    """
    recipe = project.tools / "build.json"
    if not recipe.is_file() or json.loads(recipe.read_text()) != makefile.description(project):
        return set()
    newest = recipe.stat().st_mtime_ns
    digests: dict[Path, str] = {}

    def digest(path: Path) -> str:
        if path not in digests:
            digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digests[path]

    from unbake.compilers import registry as toolchain

    for ident in {project.compiler_for(project.src / f"{name}.c").id for name in sources}:
        toolchain.verify(project.tools / ident, toolchain.specification(ident))

    reusable = set()
    for name, versions in sources.items():
        for version in versions:
            obj = generations[version] / "obj/src" / f"{name}.o"
            receipt, evidence = obj.with_suffix(".built"), obj.with_suffix(".inputs.json")
            if (
                not obj.is_file()
                or not receipt.is_file()
                or not evidence.is_file()
                or receipt.stat().st_mtime_ns < newest
            ):
                break
            saved = json.loads(evidence.read_text())
            local_source = str((project.src / f"{name}.c").relative_to(project.root))
            if local_source not in saved:
                break
            valid = True
            for word, expected in saved.items():
                path = Path(word)
                if not path.is_absolute():
                    path = project.root / path
                if not path.is_relative_to(project.root) or not path.is_file() or digest(path) != expected:
                    valid = False
                    break
            if not valid:
                break
        else:
            reusable.add(name)
    return reusable


def _prepare_version(
    shared: tuple[Project, Project, dict[str, Path], dict[str, str]], version: str
) -> list[Path] | None:
    original, staged, generations, splits = shared
    generation = generations[version]
    raw, placed = generation / f"{staged.name}.ld", generation / f"{staged.name}.link.ld"
    retained: dict[str, Any] | None = (
        {"raw": raw.read_text(), "placed": placed.read_text()} if raw.is_file() and placed.is_file() else None
    )
    after = staged.version(version).split.read_text()
    ranges = generation / "unit-ranges.json"
    if after == splits[version] and ranges.is_file():
        certified = json.loads(ranges.read_text())
        missing = extract.unit_ranges(after).keys() - certified.keys()
        if missing:
            held(
                f"submit.baseline_inputs: VERSION {version}: retained extraction omits published C: "
                + ", ".join(sorted(missing))
                + "; refresh the project build before submitting an unchanged split"
            )
    if not advance(staged, version, generation, splits[version], after):
        return None
    changed = changed_sources(original, staged, generation, version)
    if changed and (generation / "obj").is_symlink():
        staging.independent_objects(generation)
    if retained is not None:
        retained["sources"] = [source.stem for source in changed]
        retained["objects"] = {
            source.stem: staging.object_identity(generation / "obj/src" / (source.stem + ".o"))
            for source in changed
            if (generation / "obj/src" / (source.stem + ".o")).is_file()
        }
        atomic_files.text(generation / "retained-layout.json", json.dumps(retained))
    return changed


def prepare(
    original: Project,
    staged: Project,
    policy: Host,
    generations: dict[str, Path],
    splits: dict[str, str],
    *,
    submitted: Set[str] = frozenset(),
) -> dict[str, list[str]] | None:
    """Compile changed units and prove retained objects, or defer structural edits to Make."""
    # Changed global flags need the ordinary dependency graph, including assembly.
    recipe = json.loads((original.tools / "build.json").read_text())
    current = makefile.description(staged)
    if {k: v for k, v in recipe.items() if k != "units"} != {k: v for k, v in current.items() if k != "units"}:
        _miss(
            "build recipe fields changed: "
            + ", ".join(k for k in recipe if k != "units" and recipe[k] != current.get(k))
        )
        return None
    changed = {}
    shared = original, staged, generations, splits
    for version, (lines, prepared) in zip(
        generations, forked.ordered(_prepare_version, shared, list(generations), policy.cores), strict=True
    ):
        for line in lines:
            reporting.learn(line)
        if prepared is None:
            return None
        changed[version] = prepared

    compiled = build.compile_versions(
        staged,
        policy,
        {v: ([p for p in changed[v] if p.stem not in submitted], g / "obj/src") for v, g in generations.items()},
        stop_on_error=True,
    )
    for version, failures in compiled.items():
        if failures:
            name, diagnostic = next(iter(failures.items()))
            held(
                f"submit.dependencies: VERSION {version}: {staged.src / (name + '.c')}: "
                f"compile diagnostic: {diagnostic}"
            )

    # Only published prerequisites cancel the pool. Submitted sources must all
    # finish so their diagnostics can be held while successful objects prove.
    candidates = build.compile_versions(
        staged,
        policy,
        {v: ([p for p in changed[v] if p.stem in submitted], g / "obj/src") for v, g in generations.items()},
    )
    for version, failures in candidates.items():
        compiled[version].update(failures)

    def compile_version(version: str) -> tuple[list[str], dict[str, list[str]]]:
        generation = generations[version]
        intervals = extract.unit_ranges(staged.version(version).split.read_text())
        sources = list(
            dict.fromkeys(
                [*changed[version], *(staged.src / f"{name}.c" for name in sorted(submitted) if name in intervals)]
            )
        )
        snapshot = generation / "retained-layout.json"
        placement = {source.stem for source in changed[version]}
        if snapshot.is_file():
            retained = json.loads(snapshot.read_text())
            prior = retained.get("objects", {})
            for source in changed[version]:
                object_path = generation / "obj/src" / (source.stem + ".o")
                if (
                    source.stem in prior
                    and object_path.is_file()
                    and staging.object_identity(object_path) == prior[source.stem]
                ):
                    placement.discard(source.stem)
            retained["sources"] = sorted(placement)
            atomic_files.text(snapshot, json.dumps(retained))
        failures = compiled[version]
        faults = {name: [f"{version}: compile diagnostic: {reason}"] for name, reason in failures.items()}
        alignments = {
            Path(row.path).name: int(row.match["align"], 0)
            for segment in split.layout(staged.version(version).split)[2]
            for row in segment.rows
            if row.kind == "c" and row.match["align"]
        }
        image = staged.version(version).baserom.read_bytes()
        known = {line.split()[0] for line in (generation / "symbol-addresses.txt").read_text().splitlines()}
        definitions: set[str] = set()
        for source in sources:
            if source.stem in failures:
                continue
            obj = Object(generation / "obj/src" / (source.stem + ".o"))
            definitions.update(
                symbol["name"] for table in obj.symbols.values() for symbol in table if symbol["section"] != 0
            )
        args = argparse.Namespace(build=generation)
        script = (generation / f"{staged.name}.ld").read_text()
        configured = current.get("resident_mappings", {})
        mappings = layout.resident_mappings(configured.get(version, []))
        pool_path = generation / "pool-providers.json"
        pools = json.loads(pool_path.read_text()) if pool_path.is_file() else []
        pools = [row for row in pools if row["path"].startswith("rodata/")]
        for row in pools:
            mapped = [m for m in mappings if m["start"] <= row["start"] < row["end"] <= m["end"]]
            if len(mapped) > 1:
                held("layout.pool_span: ambiguous private mapping")
            row["table_entry_bias"] = mapped[0]["table_entry_bias"] if mapped else 0
        inventory = layout.resident_slices(pools, mappings) if pools else None
        for source in sources:
            name = source.stem
            if name in failures:
                continue
            obj = Object(generation / "obj/src" / (name + ".o"))
            section = obj.section(".text")
            if section is None:
                faults[name] = [f"{version}: object obj/src/{name}.o: missing .text"]
                continue
            interval = intervals[name]
            code = obj.content(section)
            target = image[interval["start"] : interval["end"]]
            relocations = {at for at, _, _ in obj.relocations(section)}
            if len(code) > len(target):
                faults.setdefault(name, []).append(
                    f"{version}: object obj/src/{name}.o: .text size {len(code)} exceeds target span {len(target)}"
                )
            alignment = alignments.get(name, 1)
            consumed = -(-(interval["address"] + len(code)) // alignment) * alignment - interval["address"]
            if len(code) < len(target) and consumed != len(target):
                faults.setdefault(name, []).append(
                    f"{version}: shift origin object obj/src/{name}.o .text: size {len(code)}, "
                    f"consumed {consumed} bytes, target span {len(target)}"
                )
            for at in range(0, min(len(code), len(target)), 4):
                if at not in relocations and code[at : at + 4] != target[at : at + 4]:
                    faults.setdefault(name, []).append(
                        f"{version}: object obj/src/{name}.o .text+0x{at:X}: "
                        f"expected {target[at : at + 4].hex()}, produced {code[at : at + 4].hex()}"
                    )
                    break
            unknown = sorted(
                {
                    symbol["name"]
                    for table in obj.symbols.values()
                    for symbol in table
                    if symbol["section"] == 0
                    and symbol["name"]
                    and symbol["info"] >> 4 == 1
                    and symbol["name"] not in known | definitions
                }
            )
            if unknown:
                faults.setdefault(name, []).append(f"{version}: undefined reference to {', '.join(unknown)}")
            if name not in placement:
                continue
            try:
                layout.place_object(
                    args,
                    f"obj/src/{name}.o",
                    script,
                    intervals,
                    image,
                    mappings,
                    [],
                    False,
                    pools=pools,
                    providers=[],
                    inventory=inventory,
                )
            except (OSError, ValueError, KeyError) as error:
                faults.setdefault(name, []).append(f"{version}: object obj/src/{name}.o: {error}")
        return [p.stem for p in sources], faults

    faults: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=min(policy.cores, len(generations))) as pool:
        futures = {v: pool.submit(compile_version, v) for v in generations}
        for version, future in futures.items():
            sources, refused = future.result()
            reporting.record("compile", version=version, sources=sources)
            for name, reasons in refused.items():
                faults.setdefault(name, []).extend(reasons)
    return faults
