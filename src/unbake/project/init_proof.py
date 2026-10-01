"""Run bootstrap tools and prove cartridge bytes before publishing."""

import subprocess
from collections.abc import Mapping
from pathlib import Path

from unbake.project.config import Held


def run(
    command: list[str], directory: Path, log: Path | None = None, *, environment: Mapping[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            command, cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError as error:
        raise Held("init", f"{command[0]}: {error}") from error
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout)
    if result.returncode:
        raise Held("init", f"{' '.join(command)}: exit {result.returncode}: {result.stdout[-6000:]}")
    return result.stdout.strip()


def proof(root: Path, name: str, version: str, data: bytes, cores: int) -> None:
    built = root / "build" / version / f"{name}.{version}.z64"
    log = root / "build" / "init" / f"{version}.log"
    run(["make", f"-j{cores}", "extract", f"VERSION={version}"], root, log)
    failure = None
    try:
        run(["make", f"-j{cores}", f"VERSION={version}", "COMPARE=1"], root, log)
    except Held as error:
        failure = error
    if not built.is_file():
        if failure:
            raise failure
        raise Held("init", f"VERSION {version}: built ROM {built.name} is missing")
    produced = built.read_bytes()
    if produced != data:
        offset = next(
            (index for index, (left, right) in enumerate(zip(data, produced, strict=False)) if left != right),
            min(len(data), len(produced)),
        )
        raise Held(
            "init",
            f"VERSION {version}: ROM mismatch at first differing offset 0x{offset:X}; "
            f"expected {data[offset : offset + 16].hex()}, produced {produced[offset : offset + 16].hex()}; "
            f"sizes {len(data)}/{len(produced)}",
        )
    if failure:
        raise failure
