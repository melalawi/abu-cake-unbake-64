"""Assignment-only evidence from retained IDO assembly intermediates."""

from __future__ import annotations

import re
from collections.abc import Mapping

from unbake.compilers.families.types import Allocation
from unbake.config import Held
from unbake.process import named as cause_named


def dump_flags() -> tuple[str, ...]:
    return ("-K", "-S")


def allocation(dumps: Mapping[str, str]) -> Allocation:
    """Read numeric hard-register operands; IDO supplies no pseudo priority or lifetime."""
    if "ido" not in dumps or not isinstance(dumps["ido"], str) or not dumps["ido"].strip():
        raise Held(
            cause_named(
                "dumps.ido",
                "dumps.ido: missing nonempty textual intermediate",
                owner="compilers.families.ido.allocation",
                stage="explain",
            )
        )
    assigned = sorted(set(int(m[1]) for m in re.finditer(r"\$(\d+)\b", dumps["ido"])))
    if not assigned:
        raise Held(
            cause_named(
                "dumps.ido.assignments",
                "dumps.ido.assignments: missing numeric register operands",
                owner="compilers.families.ido.allocation",
                stage="explain",
            )
        )
    if any(hard > 31 for hard in assigned):
        raise Held(
            cause_named(
                "dumps.ido.assignments",
                "dumps.ido.assignments: invalid general register number",
                owner="compilers.families.ido.allocation",
                stage="explain",
            )
        )
    return Allocation(
        (),
        (),
        (
            "IDO retained assembly gives hard-register operands only; "
            "pseudo assignment, priority, references and live ranges are unavailable.",
        ),
        tuple(assigned),
        "ido",
    )
