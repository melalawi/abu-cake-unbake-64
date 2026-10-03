"""Prepare one real relocatable target through the project's Makefile."""

import fcntl
import re
import shlex
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from unbake.layout import split
from unbake.project import build
from unbake.project.config import Held, Policy, Project


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
            continue
        if len(rows) != 1:
            raise Held("try", f"{function} VERSION {version}: expected one owning text row, found {len(rows)}")
        owned.append(version)
    if not owned:
        raise Held("try", f"{function}: no owning text row in selected VERSIONs")
    return owned


@contextmanager
def inputs(
    project: Project,
    function: str,
    versions: list[str],
    *,
    source: Path | None = None,
    policy: Policy | None = None,
    scratch: Path | None = None,
    read_only: bool = False,
) -> Iterator[dict[str, tuple[Path, Path]]]:
    """Pin published targets; serialize only missing inventory/object builds."""
    if not versions or len(set(versions)) != len(versions):
        raise Held("try", "versions must be nonempty and unique")
    for version in versions:
        project.version(version)
    with ExitStack() as holds:
        pinned = {}
        for version in versions:
            generation = holds.enter_context(_target_generation(project, version, read_only=read_only))
            target = target_object(project, function, version, generation=generation, read_only=read_only)
            if source is not None and policy is not None:
                from unbake.decomp import trial_entries

                target = trial_entries.target(
                    project, policy, source, version, generation, target, scratch=scratch, read_only=read_only
                )
            pinned[version] = (generation, target)
        yield pinned


@contextmanager
def _target_generation(project: Project, version: str, *, read_only: bool = False) -> Iterator[Path]:
    link = project.build_link(version)
    inventory = project.name + ".ld"

    def ready() -> bool:
        return link.is_symlink() and (link / inventory).is_file()

    if read_only:
        if not ready():
            raise Held("try", f"trial.target_missing: VERSION {version}: build on a writable copy")
        generation = build.current_generation(project, version)
        try:
            stream = (generation / ".inuse").open("rb")
        except OSError as error:
            raise Held("try", f"trial.target_pin: {generation}: {error}") from error
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            if link.resolve() != generation:
                raise Held("try", "trial.inputs_changed: generation changed while pinning; try again")
            yield generation
        return
    while True:
        if ready():
            with build.pin_current(project, version) as generation:
                if (generation / inventory).is_file():
                    yield generation
                    return
        if read_only:
            raise Held("try", f"trial.target_missing: VERSION {version}: run setup/build on a writable copy")
        with build.lock(project):
            if not ready():
                make_target(project, version, Path("build") / version / inventory)
            if not ready():
                raise Held("try", f"VERSION {version}: make produced no build inventory")


def make_target(
    project: Project, version: str, target: Path, *, changed: Path | None = None, generation: Path | None = None
) -> None:
    # Disable the Makefile's cold C batch so one target cannot compile its peers.
    command = ["make", "-j4", f"VERSION={version}", "C_COLD="]
    if changed is not None:
        command.extend(["-W", str(changed)])
    command.append(str(target))
    if generation is not None:
        command.append(f"BUILD={generation}")
    try:
        result = subprocess.run(command, cwd=project.root, capture_output=True, text=True)
    except OSError as error:
        raise Held("try", f"VERSION {version}: {shlex.join(command)}: {error}") from error
    if result.returncode:
        raise Held(
            "try", f"VERSION {version}: {shlex.join(command)} exited {result.returncode}: {result.stderr.strip()}"
        )


def target_object(
    project: Project, function: str, version: str, *, generation: Path | None = None, read_only: bool = False
) -> Path:
    """Read a pinned target, taking the writer lock only to build a missing object."""
    if generation is None:
        with _target_generation(project, version, read_only=read_only) as pinned:
            return target_object(project, function, version, generation=pinned, read_only=read_only)
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
    inventory_target = generation / (project.name + ".ld")
    inventory = set(re.findall(r'obj/(?:src|asm)/[^\s()";]+\.o', (generation / inventory_target.name).read_text()))
    if relative.as_posix() not in inventory:
        raise Held(
            "try", f"VERSION {version}: {relative} for {function} is absent from {generation / inventory_target.name}"
        )
    target = generation / relative
    if not target.is_file() and read_only:
        raise Held("try", f"trial.target_missing: {target}: build on a writable copy")
    if not target.is_file():
        with build.lock(project):
            if not target.is_file():
                # A removed object can leave a successful compile receipt behind.
                source = (project.src if row.kind == "c" else project.asm / version) / (
                    row.path + (".c" if row.kind == "c" else ".s")
                )
                changed = source.relative_to(project.root) if target.with_suffix(".built").is_file() else None
                # Publication may have moved the link while we waited for the lock.
                make_target(project, version, target, changed=changed, generation=generation)
    if not target.is_file():
        raise Held("try", f"VERSION {version}: make target build/{version}/{relative} produced no object")
    return target.resolve()
