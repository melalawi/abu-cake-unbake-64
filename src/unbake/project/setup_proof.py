"""Run bootstrap tools and prove cartridge bytes before publishing."""

import io
import os
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from unbake.config import Held, Project
from unbake.project_tools import atomic as atomic_files


@dataclass(frozen=True)
class JobSlots:
    descriptors: tuple[int, int]
    environment: dict[str, str]


@contextmanager
def job_slots(cores: int, workers: int) -> Iterator[JobSlots]:
    """Share make slots across versions, reserving one implicit slot per worker."""
    read, write = os.pipe()
    try:
        os.write(write, b"+" * (cores - workers))
        environment = dict(os.environ, MAKEFLAGS=f"--jobserver-auth={read},{write} -j")
        environment.pop("MFLAGS", None)
        environment.pop("GNUMAKEFLAGS", None)
        yield JobSlots((read, write), environment)
    finally:
        os.close(read)
        os.close(write)


def run(
    command: list[str],
    directory: Path,
    log: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    descriptors: tuple[int, ...] = (),
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=directory,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            pass_fds=descriptors,
        )
    except OSError as error:
        raise Held("setup", f"{command[0]}: {error}") from error
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        atomic_files.text(log, result.stdout)
    if result.returncode:
        raise Held("setup", f"{' '.join(command)}: exit {result.returncode}: {result.stdout[-6000:]}")
    return result.stdout.strip()


def extract(project: Project, version: str, cores: int, *, log: Path, slots: JobSlots | None = None) -> None:
    """Consume one extraction slot; keep generated inputs on disk for the proof."""
    options = [] if slots is not None else [f"-j{cores}"]
    environment = slots.environment if slots is not None else None
    descriptors = slots.descriptors if slots is not None else ()
    root = project.root
    try:
        run(
            ["make", *options, "extract", f"VERSION={version}"],
            root,
            log.with_suffix(".extract.log"),
            environment=environment,
            descriptors=descriptors,
        )
    except Held as error:
        raise Held(
            "setup", f"setup.sha1.{version}: extraction failed; log {log.with_suffix('.extract.log')}; {error.reason}"
        ) from error


def proof(
    project: Project,
    version: str,
    data: bytes | Path,
    cores: int,
    *,
    log: Path | None = None,
    slots: JobSlots | None = None,
    extracted: bool = False,
) -> None:
    root = project.root
    project.version(version)
    built = project.build_link(version) / f"{project.name}.{version}.z64"
    log = log or project.build / "setup" / f"{version}.log"
    options = [] if slots is not None else [f"-j{cores}"]
    environment = slots.environment if slots is not None else None
    descriptors = slots.descriptors if slots is not None else ()
    if not extracted:
        extract(project, version, cores, log=log, slots=slots)
    failure = None
    try:
        run(
            ["make", *options, "check", f"VERSION={version}", "COMPARE=1"],
            root,
            log,
            environment=environment,
            descriptors=descriptors,
        )
    except Held as error:
        failure = error
    if not built.is_file():
        if failure:
            raise Held("setup", f"setup.sha1.{version}: produced ROM missing; log {log}; {failure.reason}") from failure
        raise Held("setup", f"setup.sha1.{version}: built ROM {built.name} is missing; log {log}")
    with ExitStack() as stack:
        expected = stack.enter_context(data.open("rb")) if isinstance(data, Path) else io.BytesIO(data)
        produced = stack.enter_context(built.open("rb"))
        expected_size = data.stat().st_size if isinstance(data, Path) else len(data)
        produced_size = built.stat().st_size
        offset = 0
        while True:
            left, right = expected.read(1024 * 1024), produced.read(1024 * 1024)
            if left != right:
                differing = next(
                    (index for index, pair in enumerate(zip(left, right, strict=False)) if pair[0] != pair[1]),
                    min(len(left), len(right)),
                )
                offset += differing
                expected.seek(offset)
                produced.seek(offset)
                raise Held(
                    "setup",
                    f"setup.sha1.{version}: ROM mismatch at first differing offset 0x{offset:X}; "
                    f"expected {expected.read(16).hex()}, produced {produced.read(16).hex()}; "
                    f"sizes {expected_size}/{produced_size}; log {log}",
                )
            if not left:
                break
            offset += len(left)
    if failure:
        raise Held("setup", f"setup.sha1.{version}: make check failed; log {log}; {failure.reason}") from failure
