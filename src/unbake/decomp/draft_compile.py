"""Prove a draft compiles with its real unit compiler and include graph."""

from pathlib import Path

from unbake.config import Host, Project


def prove(project: Project, policy: Host, function: str, version: str, source: Path) -> None:
    """Compile the draft for one version through the runner; a refusal names the compiler diagnostic."""
    from unbake import runner

    runner.compile_unit(project, policy, source, version, unit=function, non_matching=True)
