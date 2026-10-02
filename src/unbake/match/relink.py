"""Reprove subsets with retained objects and a freshly extracted link graph."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from unbake.project import build, makefile
from unbake.project.config import Held, Policy, Project, SetupPolicy


def source_key(project: Project, policy: Policy, source: Path, version: str) -> str:
    content = build.preprocess_object(project, policy, source, version)
    # Preprocessor line directives describe locations, rather than generated code.
    content = re.sub(rb"^\s*#\s*(?:line\s+)?\d+[^\n]*\n", b"", content, flags=re.M)
    flags = [flag.replace(str(project.root), "<project>") for flag in makefile.flags(project, version, source)]
    compiler = project.compiler_for(source)
    digest = hashlib.sha256(content)
    digest.update(json.dumps([compiler.id, flags], sort_keys=True).encode())
    return digest.hexdigest()


def inputs(
    project: Project, policy: Policy, versions: list[str], generations: dict[str, Path] | None = None
) -> dict[str, dict[str, str]]:
    from unbake.project_tools.extract import unit_ranges

    return {
        version: {
            str(source.relative_to(project.root)): source_key(project, policy, source, version)
            for name in unit_ranges(project.version(version).split.read_text())
            if (source := project.src / (name + ".c")).is_file()
            and (generations is None or (generations[version] / "obj/src" / (name + ".o")).is_file())
        }
        for version in versions
    }


def prove(
    project: Project,
    policy: Policy | SetupPolicy,
    version: str,
    generation: Path,
    retained: dict[str, str] | None,
    *,
    extracted: bool = False,
    placed: bool = False,
) -> build.BuildResult:
    """Extract, verify provenance, place, link, objcopy and compare; no compilation.

    retained None means this transaction built every object from these staged sources.
    """
    log = generation / "build.log"
    image = generation / f"{project.name}.{version}.z64"
    elf = generation / f"{project.name}.elf"
    # Copied output belongs to the previous set and cannot count as fresh evidence.
    image.unlink(missing_ok=True)
    elf.unlink(missing_ok=True)
    output: list[str] = []

    def run(command: list[str], cwd: Path = project.root) -> None:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        output.extend((result.stdout, result.stderr))
        if result.returncode:
            raise Held("match", f"submit.relink.{version}: {result.stderr.strip() or result.stdout.strip()}")

    ok = False
    sha1_line = ""
    try:
        retained_inputs = {}
        if not extracted:
            if not isinstance(policy, Policy):
                raise Held("match", "submit.relink: source verification requires the work policy")
            run(["make", "extract", f"VERSION={version}", f"BUILD={generation}"])
            if retained is not None:
                retained_inputs = inputs(project, policy, [version])[version]
        for source, digest in retained_inputs.items():
            if retained is not None and retained.get(source) != digest:
                raise Held("match", f"submit.reuse_inputs: {source}: VERSION {version}: shared inputs changed")
        graph = (generation / ".split.mk").read_text()
        objects: list[str] = []
        for kind in ("C", "ASM", "ASSET"):
            match = re.search(rf"^{kind}_OBJECTS := (.*)$", graph, re.M)
            if match is None:
                raise Held("match", f"submit.relink: missing {kind} object inventory")
            objects.extend(word.replace("$(BUILD)/", "") for word in match[1].split())
        for path in objects:
            if not (generation / path).is_file():
                raise Held("match", f"submit.reuse_object: {path}: VERSION {version}: retained object missing")
        if not placed:
            run(
                [
                    sys.executable,
                    str(project.tools / "layout.py"),
                    "--script",
                    str(generation / f"{project.name}.ld"),
                    "--output",
                    str(generation / f"{project.name}.link.ld"),
                    "--build",
                    str(generation),
                    "--ranges",
                    str(generation / "unit-ranges.json"),
                    "--recipe",
                    str(project.tools / "build.json"),
                    "--version",
                    version,
                    "--baserom",
                    str(project.version(version).baserom),
                    "--non-matching",
                    "0",
                ]
            )
        recipe = makefile.recipe(project)
        ld = makefile.host_executable(policy, recipe.ld, "mips_ld")
        objcopy = makefile.host_executable(policy, recipe.objcopy, "mips_objcopy")
        scripts = re.search(r"^LINK_SCRIPTS := (.*)$", graph, re.M)
        if scripts is None:
            raise Held("match", "submit.relink: missing link scripts")
        options = [
            argument for word in scripts[1].split() for argument in ("-T", word.replace("$(BUILD)", str(generation)))
        ]
        run(
            [
                ld,
                *(generation / f"{project.name}.link.flags").read_text().split(),
                "-T",
                f"{project.name}.link.ld",
                *options,
                "-Map",
                f"{project.name}.map",
                "-o",
                str(elf),
                *objects,
            ],
            generation,
        )
        run(
            [
                objcopy,
                "-O",
                "binary",
                "--pad-to",
                str(project.version(version).baserom.stat().st_size),
                str(elf),
                str(image),
            ]
        )
        ok = hashlib.sha1(image.read_bytes()).hexdigest() == project.version(version).baserom_sha1
        sha1_line = f"{image}: {'OK' if ok else 'FAILED'}"
        output.append(sha1_line + "\n")
    except Held as error:
        output.append(error.reason + "\n")
    finally:
        log.write_text("".join(output))
        (generation / "build.exit").write_text("0\n" if ok else "1\n")
    return build.BuildResult(version, ok, sha1_line, log, generation)


def revert_rows(project: Project, generation: Path, before: str, after: str) -> bool:
    """Rewrite extracted link inputs when the split only returned C rows to assembly.

    Splat's output for such a split differs only in each unit's object path, the
    object inventory, its unit range and the symbol dump's row kind, so the
    retained objects relink without a second extraction. Anything else is False.
    """
    from unbake.layout import split

    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(old) != len(new):
        return False
    units: dict[str, str] = {}
    for left, right in zip(old, new, strict=True):
        if left == right:
            continue
        was, now = split.ROW.fullmatch(left), split.ROW.fullmatch(right)
        if was is None or now is None or (was["kind"], now["kind"]) != ("c", "asm") or was["start"] != now["start"]:
            return False
        name = Path(split.plain(was["path"])).name
        if Path(split.plain(now["path"])).name != name:
            return False
        units[name] = split.plain(now["path"])
    if not units or not all((generation / "obj/asm" / f"{path}.o").is_file() for path in units.values()):
        return False
    script = generation / f"{project.name}.ld"
    graph = generation / ".split.mk"
    ranges = generation / "unit-ranges.json"
    dump = generation / "splat_symbols.csv"
    text, inventory, table = script.read_text(), graph.read_text(), json.loads(ranges.read_text())
    rows = dump.read_text().splitlines(keepends=True)
    for name, path in units.items():
        text, count = re.subn(rf"\bobj/src/{re.escape(name)}\.o\(", f"obj/asm/{path}.o(", text)
        source, target = f"$(BUILD)/obj/src/{name}.o", f"$(BUILD)/obj/asm/{path}.o"
        lines = inventory.split("\n")
        c = next(i for i, line in enumerate(lines) if line.startswith("C_OBJECTS := "))
        a = next(i for i, line in enumerate(lines) if line.startswith("ASM_OBJECTS := "))
        words = lines[c].split(" ")
        if not count or source not in words or name not in table:
            return False
        lines[c] = " ".join(word for word in words if word != source)
        lines[a] += f" {target}"
        inventory = "\n".join(lines)
        del table[name]
        suffix = f",{name},c\n"
        rows = [row[: -len(suffix)] + f",{name},asm\n" if row.endswith(suffix) else row for row in rows]
    script.write_text(text)
    graph.write_text(inventory)
    ranges.write_text(json.dumps(table, sort_keys=True))
    dump.write_text("".join(rows))
    return True
