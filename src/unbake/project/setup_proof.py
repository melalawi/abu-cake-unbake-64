"""Run bootstrap tools and prove cartridge bytes before publishing."""

import subprocess
from collections.abc import Mapping
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


def proof(project: Project, version: str, data: bytes, cores: int, *, log: Path | None = None) -> None:
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
    produced = built.read_bytes()
    if produced != data:
        offset = next(
            (index for index, (left, right) in enumerate(zip(data, produced, strict=False)) if left != right),
            min(len(data), len(produced)),
        )
        raise Held(
            "setup",
            f"setup.sha1.{version}: ROM mismatch at first differing offset 0x{offset:X}; "
            f"expected {data[offset : offset + 16].hex()}, produced {produced[offset : offset + 16].hex()}; "
            f"sizes {len(data)}/{len(produced)}; log {log}",
        )
    if failure:
        raise Held("setup", f"setup.sha1.{version}: make check failed; log {log}; {failure.reason}") from failure
