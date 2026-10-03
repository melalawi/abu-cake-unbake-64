"""Prove source edits over a retained extraction without rebuilding unrelated units."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Set
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unbake.layout import split
from unbake.match import reporting
from unbake.match.common import held
from unbake.project import build, makefile
from unbake.project.config import Policy, Project
from unbake.project_tools import extract, layout
from unbake.project_tools.compile import dependency_paths
from unbake.project_tools.elf import Object


def _miss(reason: str) -> bool:
    reporting.record("incremental_fallback", reason=reason)
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
    definitions.write_text(bindings)
    if additions:
        with addresses.open("a") as output:
            output.writelines(f"{name} 0x{address:08X}\n" for name, address in sorted(additions.items()))
        for filename in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
            automatic = generation / filename
            automatic.write_text(extract.automatic_symbols(automatic.read_text(), additions))
    return True


def changed_sources(original: Project, staged: Project, generation: Path, version: str) -> list[Path]:
    """Use each retained object's header dependencies instead of invalidating all units."""
    sources = []
    digests: dict[Path, str] = {}
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
            dirty = not previous.is_file() or source.read_bytes() != previous.read_bytes()
            words = dependency_paths(dependencies.read_text())
            for word in words:
                path = Path(word)
                if path.is_absolute():
                    if not path.is_relative_to(original.root):
                        dirty = True
                        break
                    path = path.relative_to(original.root)
                local = staged.root / path
                if not local.is_file():
                    dirty = True
                    break
                if str(path) in saved:
                    if local not in digests:
                        digests[local] = hashlib.sha256(local.read_bytes()).hexdigest()
                    if digests[local] != saved[str(path)]:
                        dirty = True
                        break
                elif local.stat().st_mtime_ns > stamp:
                    dirty = True
                    break
            dirty |= original.compiler_for(previous).id != staged.compiler_for(source).id
        if dirty:
            sources.append(source)
    return sources


def prepare(
    original: Project,
    staged: Project,
    policy: Policy,
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
        return None
    for version, generation in generations.items():
        if not advance(staged, version, generation, splits[version], staged.version(version).split.read_text()):
            return None

    changed = {v: changed_sources(original, staged, g, v) for v, g in generations.items()}
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
        sources = changed[version]
        failures = compiled[version]
        faults = {name: [f"{version}: compile diagnostic: {reason}"] for name, reason in failures.items()}
        intervals = extract.unit_ranges(staged.version(version).split.read_text())
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
        configured = makefile.description(staged).get("resident_mappings", {})
        mappings = layout.resident_mappings(configured.get(version, []))
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
            try:
                layout.place_object(args, f"obj/src/{name}.o", script, intervals, image, mappings, [], False)
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
