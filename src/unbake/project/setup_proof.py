"""Run bootstrap tools and prove cartridge bytes before publishing."""

import io
import subprocess
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from unbake.project.config import Held, Project


def run(
    command: list[str], directory: Path, log: Path | None = None, *, environment: Mapping[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            command, cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError as error:
        raise Held("setup", f"{command[0]}: {error}") from error
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout)
    if result.returncode:
        raise Held("setup", f"{' '.join(command)}: exit {result.returncode}: {result.stdout[-6000:]}")
    return result.stdout.strip()


def proof(project: Project, version: str, data: bytes | Path, cores: int, *, log: Path | None = None) -> None:
    root = project.root
    project.version(version)
    built = project.build_link(version) / f"{project.name}.{version}.z64"
    log = log or project.build / "setup" / f"{version}.log"
    try:
        run(["make", f"-j{cores}", "extract", f"VERSION={version}"], root, log.with_suffix(".extract.log"))
    except Held as error:
        raise Held(
            "setup", f"setup.sha1.{version}: extraction failed; log {log.with_suffix('.extract.log')}; {error.reason}"
        ) from error
    failure = None
    try:
        run(["make", f"-j{cores}", "check", f"VERSION={version}", "COMPARE=1"], root, log)
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
