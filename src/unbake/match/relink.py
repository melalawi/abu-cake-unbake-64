"""Reprove subsets with retained objects and a freshly extracted link graph."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from unbake.match import reporting
from unbake.project import build, makefile
from unbake.project.config import Held, Policy, Project, SetupPolicy
from unbake.project_tools import atomic as atomic_files


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
    from unbake.project import setup

    setup.require_helpers(project)
    log = generation / "build.log"
    image = generation / f"{project.name}.{version}.z64"
    elf = generation / f"{project.name}.elf"
    # Copied output belongs to the previous set and cannot count as fresh evidence.
    image.unlink(missing_ok=True)
    elf.unlink(missing_ok=True)
    (generation / f"{project.name}.map").unlink(missing_ok=True)
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
            match = re.search(rf"^{kind}_OBJECTS :=[ \t]*(.*)$", graph, re.M)
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
        with reporting.phase("link", version=version):
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
        with reporting.phase("rom_check", version=version):
            ok = hashlib.sha1(image.read_bytes()).hexdigest() == project.version(version).baserom_sha1
        sha1_line = f"{image}: {'OK' if ok else 'FAILED'}"
        output.append(sha1_line + "\n")
    except Held as error:
        output.append(error.reason + "\n")
    finally:
        atomic_files.text(log, "".join(output))
        atomic_files.text(generation / "build.exit", "0\n" if ok else "1\n")
    return build.BuildResult(version, ok, sha1_line, log, generation)


_SELECTOR = re.compile(r"(?P<object>obj/(?:src|asm)/[^\s()]+\.o)\s*\((?P<section>[^()]+)\)")
_RESIDENT = re.compile(
    r"^  \.resident_[0-9A-F]{8} 0x[0-9A-F]{8} \(NOLOAD\) : SUBALIGN\(1\) "
    r"\{ obj/src/[^\s()]+\.o\(\.(?:rdata|rodata)\) \}\n?",
    re.M,
)


def place_changed(project: Project, version: str, generation: Path) -> bool:
    """Reuse a certified placement of unchanged objects, placing only dirty units.

    Reconstruct the retained placed script byte for byte from selector substitutions
    and resident overlays before trusting it. Any other transformation uses the
    ordinary whole-layout proof. The final link still compares the complete ROM.
    """
    import argparse

    from unbake.project_tools import layout
    from unbake.project_tools.rodata import insert_fragment

    snapshot = generation / "retained-layout.json"
    if not snapshot.is_file():
        return False
    retained = json.loads(snapshot.read_text())
    raw, placed = retained["raw"], retained["placed"]
    current = (generation / f"{project.name}.ld").read_text()
    if not retained["sources"] and current == raw:
        # No link input changed. Keep its exact placement, including transformations
        # older layout drivers used. The complete ROM is still linked and checked.
        atomic_files.text(generation / f"{project.name}.link.ld", placed)
        return True
    overlays = list(_RESIDENT.finditer(placed))
    core = _RESIDENT.sub("", placed)
    before, after = list(_SELECTOR.finditer(raw)), list(_SELECTOR.finditer(core))
    if len(before) != len(after):
        return False
    replacements: dict[str, str] = {}
    for old, new in zip(before, after, strict=True):
        if old[0] in replacements and replacements[old[0]] != new[0]:
            return False
        replacements[old[0]] = new[0]
    rendered = _SELECTOR.sub(lambda match: replacements[match[0]], raw)
    if insert_fragment(rendered, "\n".join(match[0].rstrip("\n") for match in overlays)) != placed:
        return False
    changed = {"obj/src/" + name + ".o" for name in retained["sources"]}
    current = (generation / f"{project.name}.ld").read_text()

    def rewrite(match: re.Match[str]) -> str:
        prior = replacements.get(match[0], match[0])
        target = _SELECTOR.fullmatch(prior)
        if match["object"] in changed or (target is not None and target["object"] in changed):
            return match[0]
        return prior

    script = _SELECTOR.sub(rewrite, current)
    sections = [match[0].rstrip("\n") for match in overlays if _SELECTOR.search(match[0])["object"] not in changed]  # type: ignore[index]
    intervals = json.loads((generation / "unit-ranges.json").read_text())
    image = project.version(version).baserom.read_bytes()
    configured = makefile.description(project).get("resident_mappings", {})
    mappings = layout.resident_mappings(configured.get(version, []))
    objects = {match["object"] for match in _SELECTOR.finditer(current)}
    try:
        for name in sorted(changed & objects):
            script = layout.place_object(
                argparse.Namespace(build=generation), name, script, intervals, image, mappings, sections, False
            )
    except (OSError, ValueError, KeyError):
        return False
    script = insert_fragment(script, "\n".join(sections))
    atomic_files.text(generation / f"{project.name}.link.ld", script)
    atomic_files.write(generation / f"{project.name}.link.flags", b"--no-check-sections" if sections else b"")
    reporting.record("placement", version=version, sources=sorted(changed & objects), retained=True)
    return True


def retarget_rows(project: Project, generation: Path, before: str, after: str) -> bool:
    """Transfer contiguous text spans between assembly and C without extraction.

    A C unit may own several adjacent assembly entries. Only ownership changes
    are accepted: segment fields, outer boundaries, storage rows and alignment
    remain explicit and identical. Validate everything before writing the graph.
    """
    from unbake.layout import split
    from unbake.project_tools.extract import unit_ranges

    _, old_lines, old_segments = split.parse_layout(Path("before"), before)
    _, new_lines, new_segments = split.parse_layout(Path("after"), after)
    old_rows = {row.line for segment in old_segments for row in segment.rows}
    new_rows = {row.line for segment in new_segments for row in segment.rows}
    if [line for i, line in enumerate(old_lines) if i not in old_rows] != [
        line for i, line in enumerate(new_lines) if i not in new_rows
    ]:
        return False
    if len(old_segments) != len(new_segments):
        return False
    transfers: list[tuple[list[split.Row], list[split.Row]]] = []
    for left, right in zip(old_segments, new_segments, strict=True):
        if left.fields != right.fields or left.end != right.end:
            return False
        i = j = 0
        while i < len(left.rows) and j < len(right.rows):
            was, now = left.rows[i], right.rows[j]
            if old_lines[was.line] == new_lines[now.line]:
                i += 1
                j += 1
                continue
            if was.start != now.start:
                return False
            common = {row.start for row in left.rows[i + 1 :]} & {row.start for row in right.rows[j + 1 :]}
            end = min(common) if common else left.end
            if end is None:
                return False
            a, b = i + 1, j + 1
            while a < len(left.rows) and left.rows[a].start < end:
                a += 1
            while b < len(right.rows) and right.rows[b].start < end:
                b += 1
            old, new = left.rows[i:a], right.rows[j:b]
            forward = len(new) == 1 and new[0].kind == "c" and all(row.kind == "asm" for row in old)
            reverse = len(old) == 1 and old[0].kind == "c" and all(row.kind == "asm" for row in new)
            if not (forward or reverse) or Path(was.path).name != Path(now.path).name:
                return False
            if was.match["alignment"] != now.match["alignment"] or any(
                row.match["alignment"] for row in (old[1:] if forward else new[1:])
            ):
                return False
            transfers.append((old, new))
            i, j = a, b
        if i != len(left.rows) or j != len(right.rows):
            return False
    script = generation / f"{project.name}.ld"
    graph = generation / ".split.mk"
    ranges = generation / "unit-ranges.json"
    dump = generation / "splat_symbols.csv"
    if not all(path.is_file() for path in (script, graph, ranges, dump)):
        return False
    if json.loads(ranges.read_text()) != unit_ranges(before):
        return False
    text, lines, rows = script.read_text(), graph.read_text().splitlines(), dump.read_text().splitlines(keepends=True)
    inventories = {}
    for kind in ("C", "ASM"):
        index = next((i for i, line in enumerate(lines) if line.startswith(kind + "_OBJECTS :=")), None)
        if index is None:
            return False
        inventories[kind] = (index, lines[index].split(":=", 1)[1].split())

    def object_path(row: split.Row) -> str:
        return f"obj/src/{row.path}.o" if row.kind == "c" else f"obj/asm/{row.path}.o"

    for old, new in transfers:
        old_paths, new_paths = [object_path(row) for row in old], [object_path(row) for row in new]
        old_kind, new_kind = ("C" if old[0].kind == "c" else "ASM"), ("C" if new[0].kind == "c" else "ASM")
        if new_kind == "ASM" and any(not (generation / path).is_file() for path in new_paths):
            return False
        for path in old_paths:
            word = "$(BUILD)/" + path
            if word not in inventories[old_kind][1]:
                return False
            inventories[old_kind][1].remove(word)
        inventories[new_kind][1].extend("$(BUILD)/" + path for path in new_paths)
        # The first row supplies all sections of the newly owning object; folded
        # entries' selectors disappear. Reversal restores each retained provider.
        pattern = re.escape(old_paths[0]) + r"\s*\(([^()]+)\)"

        def selectors(match: re.Match[str], paths: list[str] = new_paths) -> str:
            return ";\n        ".join(path + "(" + match[1] + ")" for path in paths)

        text, count = re.subn(pattern, selectors, text)
        if not count:
            return False
        for path in old_paths[1:]:
            text = re.sub(re.escape(path) + r"\s*\([^()]+\);?", "", text)
        aliases = {Path(row.path).name for row in (old if new_kind == "C" else new)}
        for index, row in enumerate(rows):
            fields = row.rstrip("\n").split(",")
            if len(fields) >= 2 and fields[-2] in aliases and fields[-1] in ("asm", "c"):
                fields[-1] = new[0].kind
                rows[index] = ",".join(fields) + "\n"
        if new_kind == "C":
            name = new[0].path
            lines.append(f"$(BUILD)/obj/src/{name}.built: {project.src.relative_to(project.root)}/{name}.c")
    for kind, (index, words) in inventories.items():
        lines[index] = kind + "_OBJECTS := " + " ".join(words)
    atomic_files.text(script, text)
    atomic_files.text(graph, "\n".join(lines) + "\n")
    atomic_files.text(ranges, json.dumps(unit_ranges(after), sort_keys=True))
    atomic_files.text(dump, "".join(rows))
    return True


def revert_rows(project: Project, generation: Path, before: str, after: str) -> bool:
    """Restore assembly ownership, including entries folded into one C unit."""
    return retarget_rows(project, generation, before, after)
