"""probe: measure one candidate source exactly as publish would, and keep every artifact for inspection.

The recipe is the project's own (compiler, unit options, version macros) with only the explicit overrides
the caller names. Measurement is compare.measure, so placement, hygiene and exactness are decided by the
same code publish uses. Nothing is written to the project or its ledger: all output goes into the caller's
directory and the tool's own scratch.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake import atomic, process, runner, scratch
from unbake.compilers import drivers, recipe_options
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import named as cause_named
from unbake.work import compare

DUMP = re.compile(r"-d[a-zA-Z]+")


def _refuse(key: str, reason: str) -> Held:
    return Held(cause_named(f"probe.{key}", f"probe.{key}: {reason}", owner="work.probe", stage="probe"))


def view(
    project: Project, unit: str, *, flags: tuple[str, ...], omit: tuple[str, ...], compiler: str | None
) -> Project:
    """The project seen through the probe's overrides. Defaults all come from the project's own config."""
    recipe = project.recipe_for(unit)
    ident = compiler or recipe.compiler
    if ident not in project.compilers:
        raise _refuse("compiler", f"{ident}: unknown compiler; the project declares {', '.join(project.compilers)}")
    extra = recipe_options.partition(flags)
    options = tuple(
        (phase, tuple(t for t in (*tokens, *extra[phase]) if t not in omit)) for phase, tokens in recipe.options
    )
    changed = replace(project, units={**project.units, unit: replace(recipe, compiler=ident, options=options)})
    if omit:
        selected = changed.compilers[ident]
        kept = tuple(t for t in selected.cflags if t not in omit)
        changed = replace(changed, compilers={**changed.compilers, ident: replace(selected, cflags=kept)})
    return changed


def _artifacts(
    scoped: Project, host: Host, source: Path, version: str, unit: str, dumps: tuple[str, ...], out: Path
) -> list[str]:
    """Re-run the compile stage with the dump flags beside the measured one: candidate.s and the dump files."""
    recipe = scoped.recipe_for(unit)
    extra = tuple((p, (*t, *dumps) if p == "compile" else t) for p, t in recipe.options)
    dumping = replace(scoped, units={**scoped.units, unit: replace(recipe, options=extra)})
    name = Path(unit).stem
    commands = drivers.steps(dumping, version, unit, str(source), runner.tools(host))
    text = drivers.run_preprocess(dumping, list(commands.preprocess), "compile", unit=unit)
    kept: list[str] = []
    with scratch.temporary(host, dumping, "compile", prefix="probe-") as temporary:
        work = Path(temporary)
        atomic.text(work / f"{name}.i", text)
        compile_argv = [str(dumping.compiler_for(unit).cc), *commands.compile[1:]]
        process.run_tool(compile_argv, work, "compile")
        if commands.assemble is not None:
            assembly = work / f"{name}.s"
            if assembly.is_file():
                atomic.copyfile(assembly, out / "candidate.s")
                kept.append("candidate.s")
            process.run_tool(list(commands.assemble), work, "compile")
        for path in sorted(work.iterdir()):
            if path.name not in (f"{name}.i", f"{name}.s", f"{name}.o"):
                (out / "dumps").mkdir(exist_ok=True)
                atomic.copyfile(path, out / "dumps" / path.name)
                kept.append(f"dumps/{path.name}")
    return kept


def _disassemble(host: Host, binary: Path, listing: Path) -> None:
    argv = [str(host.mips_objdump), "-D", "-b", "binary", "-m", "mips:4300", "-EB", str(binary)]
    atomic.text(listing, process.run_tool(argv, binary.parent, "probe"))


def findings(source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Unmarked source-rule findings and, among them, the volatile ones (never landable)."""
    from unbake.decomp import checks

    broken = [f for f in checks.run(source) if f.fakematch is None]
    rows = [{"rule": f.rule, "line": f.line, "text": f.text, "sentence": checks.plain(f)} for f in broken]
    return rows, [row for row in rows if row["rule"] == "volatile-storage"]


def run(
    project: Project,
    host: Host,
    function: str,
    source: Path,
    out: Path,
    *,
    version: str | None = None,
    flags: tuple[str, ...] = (),
    omit: tuple[str, ...] = (),
    compiler: str | None = None,
) -> dict[str, Any]:
    from unbake.work.source_scope import scoped_project

    source, out = source.resolve(), out.resolve()
    if not source.is_file():
        raise _refuse("source", f"{source}: missing file")
    if source.is_relative_to(project.work) or out.is_relative_to(project.root):
        raise _refuse("scope", f"{out}: probe output and source stay outside the project's work and src trees")
    holding = split.holding_versions(project, function)
    version = version or holding[0]
    if version not in holding:
        raise _refuse("version", f"{version}: {function} is held by {', '.join(holding)}")
    dumps = tuple(f for f in flags if DUMP.fullmatch(f))
    measured_flags = tuple(f for f in flags if f not in dumps)
    out.mkdir(parents=True, exist_ok=True)
    candidate = out / f"{function}.c"
    if candidate != source:
        atomic.copyfile(source, candidate)
    unit = project.unit_path(function)
    base = view(project, unit, flags=measured_flags, omit=omit, compiler=compiler)
    scoped = scoped_project(base, candidate, (out,), ())
    unit = scoped.unit_path(candidate)
    row = compare.row_of(scoped, function, version)
    target = split.words(scoped, row)
    atomic.write(out / "original.bin", target)
    placed: dict[str, Any] = {}

    def keep(v: str, obj: Path, linked: bytes, problems: list[str], info: dict[str, Any]) -> None:
        atomic.copyfile(obj, out / "candidate.o")
        atomic.write(out / "linked.bin", linked)
        placed.update(problems=list(problems), info=info)

    compared = compare.measure(scoped, host, candidate, versions=(version,), on_linked=keep)
    result = compared.compares[version]
    files = [name for name in ("candidate.o", "linked.bin", "original.bin") if (out / name).is_file()]
    files += _artifacts(scoped, host, candidate, version, unit, dumps, out)
    for name in ("original", "linked"):
        if (out / f"{name}.bin").is_file():
            _disassemble(host, out / f"{name}.bin", out / f"{name}.dis")
            files.append(f"{name}.dis")
    problems = placed.get("problems", [])
    rules, volatile = findings(candidate.read_text())
    landable = not rules
    document = {
        "function": function,
        "version": version,
        "source": str(source),
        "candidate": str(candidate),
        "compiler": compared.compiler,
        "overrides": {"flags": list(flags), "omit": list(omit), "compiler": compiler},
        "commands": {
            key: placed.get("info", {}).get(key) for key in ("preprocess_argv", "compile_argv", "assemble_argv")
        },
        "measurement": result.document(),
        "identical_words": result.identical_words,
        "target_words": result.target_words,
        "typed": result.typed,
        "placement_problems": problems,
        "source_hygiene": rules,
        "volatile": volatile,
        "landable": landable,
        "exact": compare.acceptance(compared.compares, (version,), compared.preconditions)
        and not problems
        and landable,
        "files": sorted(files),
        "seconds": round(compared.seconds, 3),
    }
    atomic.text(out / "result.json", json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document
