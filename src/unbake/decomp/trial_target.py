"""Prepare one real relocatable target through the project's Makefile."""

import re
import shlex
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from unbake.layout import split
from unbake.project import build
from unbake.project.config import Held, Project
from unbake.project_tools.elf import Object


def require_symbol_boundary(project: Project, function: str, version: str, target: Path) -> None:
    """Refuse an object-symbol cut inside its owning row before classifying code.

    A second function symbol inside one split unit can truncate objdiff's entry
    even though the ROM interval is intact. Repair belongs to split boundary-map.
    Ordinary candidate size differences remain instruction differences.
    """
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1:
        return
    owner = rows[0]
    obj = Object(target)
    symbols = [s for table in obj.symbols.values() for s in table if s["info"] & 15 == 2 and s["section"]]
    entries = [s for s in symbols if s["name"] == function]
    if not entries:
        entries = [s for s in symbols if s["value"] == 0]
    if len(entries) != 1:
        return
    entry = entries[0]
    size = owner.end - owner.start
    following = [
        s
        for s in symbols
        if s["section"] == entry["section"]
        and entry["value"] < s["value"] < entry["value"] + size
        and s["value"] >= entry["value"] + entry["size"]
    ]
    if not entry["size"] or entry["size"] >= size or not following:
        return
    names = ", ".join(s["name"] for s in sorted(following, key=lambda s: s["value"]))
    raise Held(
        "try",
        f"precondition split-boundary: {function} VERSION {version}: target function symbol covers "
        f"0x{entry['size']:X} bytes of owning text row 0x{size:X}; remaining bytes belong to {names}; "
        "resolve the split boundary with split boundary-map before try",
    )


def owning_versions(project: Project, function: str, versions: list[str] | None) -> list[str]:
    """Select only split-owned functions; a symbol alone can be stale or data."""
    selected = list(project.versions) if versions is None else list(versions)
    if not selected or len(set(selected)) != len(selected):
        raise Held("try", "versions must be nonempty and unique")
    owned = []
    for version in selected:
        project.version(version)
        rows = [row for row in split.functions(project, version) if function in row.aliases]
        if not rows:
            print(f"ABSENT(try): {function} VERSION {version}: no owning text row")
            continue
        if len(rows) != 1:
            raise Held("try", f"{function} VERSION {version}: expected one owning text row, found {len(rows)}")
        owned.append(version)
    if not owned:
        raise Held("try", f"{function}: no owning text row in selected VERSIONs")
    return owned


@contextmanager
def inputs(project: Project, function: str, versions: list[str]) -> Iterator[dict[str, tuple[Path, Path]]]:
    """Build missing targets, then pin their generations before releasing the writer lock."""
    if not versions or len(set(versions)) != len(versions):
        raise Held("try", "versions must be nonempty and unique")
    for version in versions:
        project.version(version)
    with ExitStack() as holds:
        pinned = {}
        with build.lock(project):
            for version in versions:
                target = target_object(project, function, version)
                generation = holds.enter_context(build.pin(build.current_generation(project, version)))
                pinned[version] = (generation, target)
        yield pinned


def make_target(project: Project, version: str, target: Path, *, changed: Path | None = None) -> None:
    # Disable the Makefile's cold C batch so one target cannot compile its peers.
    command = ["make", "-j4", f"VERSION={version}", "C_COLD="]
    if changed is not None:
        command.extend(["-W", str(changed)])
    command.append(str(target))
    try:
        result = subprocess.run(command, cwd=project.root, capture_output=True, text=True)
    except OSError as error:
        raise Held("try", f"VERSION {version}: {shlex.join(command)}: {error}") from error
    if result.returncode:
        raise Held(
            "try", f"VERSION {version}: {shlex.join(command)} exited {result.returncode}: {result.stderr.strip()}"
        )


def target_object(project: Project, function: str, version: str) -> Path:
    """Resolve the owning text row, then build only its missing inventory/object."""
    configured = project.version(version)
    _, _, segments = split.layout(configured.split)
    _, symbols = split.symbols(configured.symbols)
    address = symbols[function][0] if function in symbols else None
    rows = [
        row
        for segment in segments
        for row in segment.rows
        if row.kind in ("asm", "c")
        and (
            split.address(row, configured.split)
            <= address
            < split.address(row, configured.split) + split.end(row) - row.start
            if address is not None
            else Path(row.path).name == function
        )
    ]
    if len(rows) != 1:
        raise Held(
            "try", f"VERSION {version}: target object for {function}: expected one owning text row, found {len(rows)}"
        )
    row = rows[0]
    relative = Path("obj") / ("src" if row.kind == "c" else "asm") / (row.path + ".o")
    link = project.build_link(version)
    inventory_target = Path("build") / version / (project.name + ".ld")
    if not link.is_symlink() or not (link / ".split.mk").is_file() or not (link / inventory_target.name).is_file():
        make_target(project, version, inventory_target)
    generation = build.current_generation(project, version)
    inventory = set(re.findall(r'obj/(?:src|asm)/[^\s()";]+\.o', (generation / inventory_target.name).read_text()))
    if relative.as_posix() not in inventory:
        raise Held(
            "try", f"VERSION {version}: {relative} for {function} is absent from {generation / inventory_target.name}"
        )
    target = generation / relative
    if not target.is_file():
        # A removed object can leave a successful compile receipt behind. Tell
        # make its source changed so that the receipt's own recipe runs again.
        source = (project.src if row.kind == "c" else project.asm / version) / (
            row.path + (".c" if row.kind == "c" else ".s")
        )
        changed = source.relative_to(project.root) if target.with_suffix(".built").is_file() else None
        make_target(project, version, Path("build") / version / relative, changed=changed)
    if not target.is_file():
        raise Held("try", f"VERSION {version}: make target build/{version}/{relative} produced no object")
    return target.resolve()
