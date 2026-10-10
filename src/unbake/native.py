"""Resource-driven native object production and per-member ROM proofs."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

import abucache
from abucache import compile

from unbake import adapters, build, config, effort, process, recipes, store, symptoms, versions, view
from unbake.contracts import (
    Config,
    Finding,
    NativeResult,
    Origin,
    Placement,
    Proof,
    Recipe,
    Refusal,
    Snapshot,
    UnitSpec,
    digest,
)

_WORD = 0xFFFFFFFF
@contextmanager
def _tools(unit: UnitSpec):
    try:
        yield
    except Refusal as error:
        raise Refusal(*(replace(f, reason=f"{f.reason}; unit kind {unit.kind}")
                        if f.key == "native.missing_tool" else f for f in error.findings)) from error
def _phases(unit: UnitSpec) -> tuple[str, ...]:
    return tuple(config.load_resource("units.toml")["kind"][unit.kind]["phases"])
def _held(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[tuple[str, Placement], ...]:
    return tuple((name, p) for name in unit.members for p in snapshot.layout.members[name].placements
                 if p.version == version)
def sections(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[Placement, ...]:
    """One contiguous ROM/memory range per section owned by this unit in a holder."""
    held, output = _held(snapshot, unit, version), []
    for section in sorted({p.section for _, p in held}):
        placements = sorted((p for _, p in held if p.section == section), key=lambda p: p.vram)
        first, last = placements[0], placements[-1]
        if any(a.vram + a.size != b.vram or (section != ".bss" and a.rom_end != b.rom_start)
               for a, b in pairwise(placements)):
            raise Refusal(Finding("link.error", f"owned {section} run is not contiguous", unit=unit.path))
        vram = first.vram & snapshot.config.project.build.get("text_mask", _WORD) if section == ".text" else first.vram
        output.append(Placement(version, section, first.rom_start, last.rom_end, vram,
                                sum(p.size for p in placements) if section == ".bss" else 0))
    return tuple(output)
_CODE = digest([Path(m.__file__).read_bytes() for m in (adapters, build, symptoms, versions, view)])
def stamp(snapshot: Snapshot, unit: UnitSpec, version: str) -> str | None:
    """Everything a proof reads, named without building: unit, reachable files, rows, version facts and tools. None
    when a build must answer (an overlay holds one of the files)."""
    cfg, recipe = snapshot.config, recipes.resolve(snapshot.config, unit, {})
    reach, record = view.closure(snapshot, unit, version), snapshot.versions[version]
    if reach is None:
        return None
    tools = effort.memo(("tools", recipe.toolchain), lambda: (adapters.host_tools(cfg), adapters.tool_identity(
        cfg, recipe.toolchain)))
    return digest((replace(unit, withheld=()), reach, recipe.digest, _held(snapshot, unit, version),
                   [snapshot.layout.members[n].state for n in unit.members], record.rom_sha256,
                   versions.facts_digest(record), cfg.project.build, cfg.project.version_macros[version],
                   tools, abucache.__version__, _CODE))
def _gaps(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe,
          object_sha: str, missing: tuple[str, ...]) -> tuple[Proof, ...]:
    source_sha = sha256(snapshot.read(unit.path)).hexdigest()
    return tuple(
        Proof(unit.path, name, version, recipe.digest, source_sha, object_sha,
              "", "", False, missing, 0.0, symptoms.from_missing(missing))
        for name in dict.fromkeys(n for n, _ in _held(snapshot, unit, version))
    )
def _replayed(cfg: Config, key: str, produce):
    """A failed build is as final as an object: its refusal is stored under the key and replayed."""
    refused = store.get(cfg, "refused", key)
    if refused is not None:
        effort.count("refused", True)
        raise Refusal(*(_finding(row) for row in json.loads(refused)))
    try:
        return produce()
    except Refusal as refusal:
        effort.count("refused", False)
        store.put(cfg, "refused", key, json.dumps([asdict(f) for f in refusal.findings]).encode())
        raise
def _compile_inputs(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe,
                    include: tuple[Path, ...], stem: str) -> tuple[str, tuple[compile.Step, ...], str, str]:
    text = view.get(snapshot, unit, version, recipe, lines=False).text
    steps = adapters.chain_steps(snapshot.config, recipe.toolchain, recipe, include, stem)
    tools = str(effort.memo(("tool-identity", recipe.toolchain),
                            lambda: adapters.tool_identity(snapshot.config, recipe.toolchain)))
    return text, steps, tools, compile.key(text.encode(), f"{stem}.i", steps, tools)
def _compiled(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, work: Path, include: tuple[Path, ...],
              stem: str) -> tuple[Path, tuple[NativeResult, ...]]:
    """Preprocess, compile and assemble through abucache's cache, keyed by the preprocessed bytes, the exact commands
    and the hash of the tools. Every version whose preprocessed bytes are identical shares one object."""
    cfg = snapshot.config
    if "preprocess" not in _phases(unit):
        raise Refusal(Finding("config.resource", f"unit kind {unit.kind} is cacheable but has no preprocess phase"))
    text, steps, tools, key = _compile_inputs(snapshot, unit, version, recipe, include, stem)
    source, obj = work / f"{stem}.i", work / f"{stem}.o"
    source.write_text(text, encoding="utf-8")
    def run(phase: str, argv: Sequence[str], cwd: Path) -> NativeResult:
        return process.run(phase, argv, cwd, tmp=process.scratch(cfg.project.root))
    value, results = _replayed(cfg, key, lambda: compile.build(store.content(cfg), run, steps, source, tools, work,
                                                              after=adapters.toolchain(cfg, recipe.toolchain).check))
    obj.write_bytes(value)
    if results:  # the compile that ran keeps its refused warnings, so nothing compiles again to count them
        found = adapters.toolchain(cfg, recipe.toolchain).refused(results[0].stderr.decode(errors="replace"))
        store.put(cfg, "warnings", digest((key, "warnings")), json.dumps(found).encode())
    return obj, tuple(results)
def recorded(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe) -> tuple[str, ...] | None:
    """The refused warnings the build or a proof kept for this unit; None when none was kept; never compiles."""
    cfg = snapshot.config
    if not config.load_resource("units.toml")["kind"][unit.kind]["cacheable"]:
        return ()
    home = cfg.project.root / "build" / version / Path(unit.path).with_suffix("")
    for kept in sorted(home.glob("*/compile.err")):
        return tuple(adapters.toolchain(cfg, recipe.toolchain).refused(kept.read_text(errors="replace")))
    *_, key = _compile_inputs(snapshot, unit, version, recipe, _include(snapshot, unit, version), Path(unit.path).stem)
    kept_json = store.get(cfg, "warnings", digest((key, "warnings")))
    return None if kept_json is None else tuple(json.loads(kept_json))
def warnings(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, work: Path) -> tuple[str, ...]:
    """The refused-view warnings of the unit's own compile, kept by the text compiled."""
    with effort.stage("native.warnings"), _tools(unit):
        cfg = snapshot.config
        if not config.load_resource("units.toml")["kind"][unit.kind]["cacheable"]:
            return ()
        work.mkdir(parents=True, exist_ok=True)
        stem = Path(unit.path).stem
        text, steps, _, key = _compile_inputs(snapshot, unit, version, recipe, _include(snapshot, unit, version), stem)
        def _produce() -> bytes:
            (work / f"{stem}.i").write_text(text, encoding="utf-8")
            argv = [w.replace("{in}", f"{stem}.i").replace("{out}", steps[0].out) for w in steps[0].argv]
            result = process.run("compile", argv, work, tmp=process.scratch(cfg.project.root))
            found = adapters.toolchain(cfg, recipe.toolchain).refused(result.stderr.decode(errors="replace"))
            return json.dumps(found).encode()
        return tuple(json.loads(store.cached(cfg, "warnings", digest((key, "warnings")), _produce)))
def _include(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[Path, ...]:
    project = snapshot.config.project
    macros = config.load_resource("repo.toml")["splat"]["options"]["generated_asm_macros_directory"]
    return (project.root / "include", (project.root / unit.path).parent,
            project.root / macros.format(version=version, name=project.name))
def objects(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe,
            work: Path) -> tuple[Path, tuple[NativeResult, ...]]:
    with effort.stage("native.objects"), _tools(unit):
        work.mkdir(parents=True, exist_ok=True)
        phases = _phases(unit)
        row = config.load_resource("toolchains.toml")["toolchain"][recipe.toolchain]
        source = snapshot.read(unit.path)
        stem = Path(unit.path).stem
        obj = work / f"{stem}.o"
        results: list[NativeResult] = []
        include = _include(snapshot, unit, version)
        if config.load_resource("units.toml")["kind"][unit.kind]["cacheable"]:
            return _compiled(snapshot, unit, version, recipe, work, include, stem)
        dependencies, pending = {}, [source]
        while pending:
            for name in re.findall(rb'\.include\s+"([^"\n]+)"', pending.pop()):
                path = next((p / name.decode() for p in include if (p / name.decode()).is_file()), None)
                if path and str(path) not in dependencies:
                    data = path.read_bytes()
                    dependencies[str(path)] = sha256(data).hexdigest()
                    pending.append(data)
        key = digest((sha256(source).hexdigest(), recipe.digest, recipe.toolchain, row["pins"],
                      sorted(dependencies.items())))

        def _produce() -> bytes:
            current = None
            for phase in (p for p in phases if p in ("assemble", "armips")):
                if current is None:
                    current = work / f"{stem}.s"
                    current.write_bytes(source)
                out = obj if phase == "assemble" else work / f"{stem}.bin"
                results.append(adapters.assembler(snapshot.config, "gnu" if phase == "assemble" else phase)
                               .assemble(current, recipe, include, out))
                current = out
            return current.read_bytes()
        obj.write_bytes(_replayed(snapshot.config, key, lambda: store.cached(snapshot.config, "object", key, _produce)))
        return obj, tuple(results)
def _finding(row: dict) -> Finding:
    origin = Origin(**row["origin"]) if row["origin"] else None
    return Finding(**{**row, "origin": origin, "versions": tuple(row["versions"]), "missing": tuple(row["missing"])})
def measure(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe,
            obj: Path, work: Path) -> tuple[Proof, ...]:
    with effort.stage("native.measure"), _tools(unit):
        ranges = sections(snapshot, unit, version)
        if not ranges:
            return ()
        object_sha = sha256(obj.read_bytes()).hexdigest()
        source_sha = sha256(snapshot.read(unit.path)).hexdigest()
        record = snapshot.versions[version]
        names = versions.undefined(obj) if "armips" not in _phases(unit) else ()
        selected = replace(record, symbols={n: record.symbols[n] for n in names if n in record.symbols})
        symbols_data = build.symbols_ld(replace(snapshot, versions={**snapshot.versions, version: selected}), version)
        row = config.load_resource("toolchains.toml")["toolchain"][recipe.toolchain]
        trim = not row["preserves_padding"] or snapshot.layout.members[unit.members[0]].state == "hasm"
        held = [(n, asdict(p)) for n, p in _held(snapshot, unit, version)]  # member names are part of the proof
        key = digest((object_sha, [asdict(p) for p in ranges], held,
                      sha256(symbols_data).hexdigest(), trim,
                      abucache.__version__,
                      digest(sorted((k, v) for k, v in adapters.host_tools(snapshot.config).items()
                                    if k in ("mips_ld", "mips_objcopy"))),
                      unit.path, recipe.digest, source_sha))
        def _produce() -> bytes:
            built, proofs = {}, []
            if "armips" in _phases(unit):
                if len(ranges) != 1 or ranges[0].section == ".bss":
                    gaps = _gaps(snapshot, unit, version, recipe, object_sha,
                                 (f"version {version}: unproved resource sections",))
                    return json.dumps([asdict(p) for p in gaps], sort_keys=True).encode()
                built[ranges[0].section] = obj.read_bytes()
            else:
                work.mkdir(parents=True, exist_ok=True)
                symbols = work / "symbols.ld"
                symbols.write_bytes(symbols_data)
                result = adapters.linker(snapshot.config).link(obj, ranges, symbols, work, trim)
                built = {s: p.read_bytes() for s, p in result.outputs.items() if s.startswith(".")}
            by_section = {p.section: p for p in ranges}
            for name in dict.fromkeys(n for n, _ in _held(snapshot, unit, version)):
                material, target, missing, measured = [], [], [], []
                for member, p in _held(snapshot, unit, version):
                    if member != name:
                        continue
                    base, data = by_section[p.section], built.get(p.section)
                    if data is None:
                        missing.append(f"version {version}: unproved {p.section}")
                        continue
                    if p.section == ".bss":
                        if data != str(base.size).encode():
                            missing.append(f"version {version}: unproved .bss size")
                        material.append(data)
                        target.append(str(base.size).encode())
                        continue
                    b = data[p.rom_start-base.rom_start:p.rom_end-base.rom_start]
                    t = versions.rom_bytes(snapshot.versions[version], p.rom_start, p.rom_end)
                    measured.append((b, t, p.section))
                    if len(data) != base.size:
                        missing.append(f"version {version}: {p.section} size {len(data)} != {base.size}")
                    differences = [i for i, (x, y) in enumerate(zip(b, t, strict=False)) if x != y]
                    if differences:
                        missing.append(f"version {version}: {len(differences)} bytes differ "
                                       f"at +0x{differences[0]:X} {p.section}")
                    material.append(b)
                    target.append(t)
                exact = not missing
                score = 1.0 if exact else min(symptoms.score(b"".join(b for b, _, _ in measured),
                                                           b"".join(t for _, t, _ in measured)), 1.0 - 2**-53)
                facts = {} if exact else dict(symptoms.merge(
                    [symptoms.measure(b, t, s) for b, t, s in measured] + [symptoms.from_missing(missing)]))
                if not exact:
                    facts["score"] = score
                proof = Proof(unit.path, name, version, recipe.digest, source_sha, object_sha,
                              sha256(b"".join(material)).hexdigest(), sha256(b"".join(target)).hexdigest(),
                              exact, tuple(missing), score, facts)
                proofs.append(proof)
                store.put(snapshot.config, "bytes", proof.built_sha256, b"".join(material))
            return json.dumps([asdict(p) for p in proofs], sort_keys=True).encode()
        return tuple(Proof(**{**p, "missing": tuple(p["missing"])})
                     for p in json.loads(store.cached(snapshot.config, "proof", key, _produce)))
def prove_args(item: tuple) -> tuple[Proof, ...]:
    return prove(*item)
def prove_job(item: tuple[Snapshot, UnitSpec, str]) -> tuple[Proof, ...]:
    """A pool job: the proof of the unit in one version, in a private work directory."""
    snapshot, unit, version = item
    with store.work(snapshot.config) as work:
        return prove(snapshot, unit, version, recipes.resolve(snapshot.config, unit, {}), work)
def prove(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe,
          work: Path) -> tuple[Proof, ...]:
    with effort.stage("native.prove"):
        object_sha = ""
        try:
            obj, _ = objects(snapshot, unit, version, recipe, work)
            object_sha = sha256(obj.read_bytes()).hexdigest()
            names = versions.undefined(obj) if "armips" not in _phases(unit) else ()
            record = snapshot.versions[version]
            findings = versions.resolve(record, names, unit.path)
            if findings:
                missing = tuple(f"version {version}: unresolved {name}"
                                for name in names if name not in record.symbols)
                return _gaps(snapshot, unit, version, recipe, object_sha, missing)
            return measure(snapshot, unit, version, recipe, obj, work)
        except Refusal as refusal:
            if any(f.key == "native.missing_tool" for f in refusal.findings):
                raise
            missing = tuple(f"version {version}: {finding.key}: " + view.diagnostic(
                snapshot, unit, version, recipe, finding.reason) for finding in refusal.findings)
            return _gaps(snapshot, unit, version, recipe, object_sha, missing)
