"""Measure one unit in every holding version and report the gaps against the ROMs."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import crack, effort, land, layout, native, ownership, pool, process, recipes, store, symptoms, versions
from unbake.contracts import Config, Finding, Json, Proof, Refusal, Snapshot, UnitSpec


def holders(snapshot: Snapshot, unit: UnitSpec) -> tuple[str, ...]:
    return tuple(sorted({v for name in unit.members for v in snapshot.layout.members[name].holders()}))


def _export(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Any, work: Path,
            proofs: Sequence[Proof], target: Path) -> None:
    target.mkdir(parents=True)
    stem = Path(unit.path).stem
    for name in (f"{stem}.i", f"{stem}.s", f"{stem}.o", "linked.elf", "text.bin",
                 "rodata.bin", "data.bin", "bss.size"):
        if (work / name).exists():
            shutil.copy2(work / name, target / name)
    for extra in sorted(list(work.glob("*.greg")) + list(work.glob("*.lreg"))):
        shutil.copy2(extra, target / extra.name)
    held = sorted(
        (p for n in unit.members for p in snapshot.layout.members[n].placements
         if p.version == version and p.section == ".text"),
        key=lambda p: p.rom_start,
    )
    if held:
        first, last = held[0], held[-1]
        original = versions.rom_bytes(snapshot.versions[version], first.rom_start, last.rom_end)
        (target / "original.bin").write_bytes(original)
        objdump = process.tool(snapshot.config, "mips_objdump")
        for source, name in (("text.bin", "linked.dis"), ("original.bin", "original.dis")):
            if (target / source).exists():
                process.run("compare.disassemble",
                            [str(objdump), "-D", "-b", "binary", "-m", "mips", "-EB",
                             f"--adjust-vma=0x{first.vram:X}", str(target / source)],
                            target, stdout_path=target / name, tmp=process.scratch(snapshot.config.project.root))
    document = {"recipe": asdict(recipe), "proofs": [asdict(p) for p in proofs if p.version == version]}
    (target / "result.json").write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

def measure(snapshot: Snapshot, unit: UnitSpec, overrides: Json, out: Path | None) -> tuple[Proof, ...]:
    return measure_many(snapshot, unit, [overrides], out)[0]

def measure_many(snapshot: Snapshot, unit: UnitSpec, variants: Sequence[Json],
                 out: Path | None = None) -> list[tuple[Proof, ...]]:
    """One proof tuple per override variant, all variants and versions in one pool map; `out` exports variant 0."""
    with effort.stage("compare.measure"):
        config = snapshot.config
        if out is not None and out.exists():
            raise Refusal(Finding("compare.out", reason=f"{out} already exists", unit=unit.path,
                                  action="pass a new --out directory"))
        recipes_ = [recipes.resolve(config, unit, o) for o in variants]
        snapshot, unit = ownership.derive(snapshot, unit)  # the data its source emits is part of what must match
        held = holders(snapshot, unit)
        with store.work(config) as work:
            results = pool.map(config, "compare.measure", native.prove_args,
                               [(snapshot, unit, v, r, work / (v if i == 0 else f"{i}-{v}"))
                                for i, r in enumerate(recipes_) for v in held])
            proofs = [tuple(p for batch in results[i * len(held):(i + 1) * len(held)] for p in batch)
                      for i in range(len(variants))]
            if out is not None:
                staging = out.parent / (".tmp-" + out.name)
                if staging.exists():
                    shutil.rmtree(staging)
                for v in held:
                    _export(snapshot, unit, v, recipes_[0], work / v, proofs[0], staging / v)
                os.replace(staging, out)
        return proofs

def gaps(snapshot: Snapshot, unit: UnitSpec, proofs: Sequence[Proof]) -> tuple[Finding, ...]:
    found = {(p.member, p.version): p for p in proofs}
    findings = []
    for name in unit.members:
        bad: list[str] = []
        badproofs: list[Proof] = []
        lost: list[str] = []
        for v in snapshot.layout.members[name].holders():
            proof = found.get((name, v))
            if proof is None:
                bad.append(v)
                lost.append(f"version {v}: no measurement")
            elif not proof.exact:
                bad.append(v)
                badproofs.append(proof)
                lost.extend(proof.missing)
        if lost:
            facts = symptoms.merge([p.symptoms for p in badproofs] + [symptoms.from_missing(lost)])
            findings.append(Finding("land.not_exact", reason=f"{name} does not match the ROM in every holding version",
                                    unit=name, versions=tuple(bad), missing=tuple(lost), symptoms=facts))
    return tuple(findings)

def bind(snapshot: Snapshot, file: Path, function: str | None) -> tuple[UnitSpec, Snapshot]:
    root = snapshot.config.project.root.resolve()
    path = file.resolve()
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
    if rel in snapshot.layout.units:
        return snapshot.layout.units[rel], snapshot
    if not file.is_file():
        raise Refusal(Finding("land.request", reason=f"the file {file} does not exist", path=str(file),
                              action="pass an existing source file"))
    member = function or file.stem
    if member not in snapshot.layout.members:
        raise Refusal(Finding("land.request", reason=f"{member} is not a member of the layout",
                              action="pass --function NAME"))
    units = sorted(snapshot.layout.units.values(), key=lambda u: u.path)
    landed = next((u for u in units if member in u.members), None)
    if landed:  # a new text for source that is already landed replaces that unit's file
        return landed, layout.overlay(snapshot, {landed.path: file.read_bytes()})
    options = layout.unit_options(snapshot, member, file.read_bytes())
    if not options:
        raise Refusal(Finding("land.request", reason=f"{member} has no standalone unit option",
                              action="pass --function NAME"))
    unit, writes = options[-1]
    return unit, layout.overlay(snapshot, writes)

def run(config: Config, params: Json) -> Json:
    with effort.stage("compare.run"):
        snapshot = layout.capture(config)
        unit, bound = bind(snapshot, Path(params["file"]), params["function"])
        overrides: dict[str, Any] = {"add": list(params["flag"]), "omit": list(params["omit_flag"])}
        if params["toolchain"]:
            overrides["toolchain"] = params["toolchain"]
        out = Path(params["out"]) if params["out"] else None
        bound, unit = ownership.derive(bound, unit)
        proofs = measure(bound, unit, overrides, out)
        found = gaps(bound, unit, proofs)
        member = params["function"] or (unit.members[0] if len(unit.members) == 1 else None)
        if member is None:
            raise Refusal(Finding("land.request", reason="the file holds several members; pass --function",
                                  unit=unit.path, action="pass --function NAME"))
        note = params["note"] or ""
        _, feedback = crack.feedback(bound, member, "creative", proofs, note)
        request = {"file": str(params["file"]), "function": member, "overrides": overrides, "note": note}
        submission = land.submit(config, request, unit, proofs, Path(params["file"]).read_bytes(), "compare")
        drain = land.drain(config) if submission else None
        score = {v: min(p.score for p in proofs if p.version == v) for v in sorted({p.version for p in proofs})}
        return {"unit": unit.path, "members": list(unit.members),
                "exact": all(p.exact for p in proofs) and not found, "score": score,
                "proofs": [asdict(p) for p in proofs], "gaps": [asdict(f) for f in found],
                "feedback": feedback, "submitted": submission.id if submission else None, "drain": drain}
