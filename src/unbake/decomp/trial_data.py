"""Judge unpublished data identities with submit's owning-ROM inference."""

import re
from pathlib import Path

from unbake.match import data_symbols
from unbake.config import Held, Host, Project


def infer(project: Project, policy: Host, function: str, version: str, candidate: Path) -> dict[str, int]:
    pending = data_symbols.needs(project, function, version, candidate)
    for need in pending:
        shaped = re.fullmatch(r"D_([0-9A-Fa-f]{8})", need.name)
        if shaped and int(shaped[1], 16) != need.address:
            raise Held(
                "try",
                f"trial.data_symbols: {need.name}: address-shaped name placed at "
                f"0x{need.address:08X}; use this version's name or a cross-version identity",
            )
    if pending:
        data_symbols.resolve(pending, project, policy)
    return {need.name: need.address for need in pending}
