"""One pure hint projection from pinned subsystem and chosen compare evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from unbake.layout.subsystems import Snapshot
from unbake.search import BUILTINS


@dataclass(frozen=True)
class Hint:
    subject: str
    snapshot_key: str
    evidence_ids: tuple[str, ...]
    hypothesis: str
    provider_ids: tuple[str, ...]
    consumer_ids: tuple[str, ...]
    applicable_methods: tuple[str, ...]
    disconfirming_observation: str
    next_existing_action: tuple[str, ...]
    mismatch_refs: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return asdict(self)


def for_subject(subject: str, snapshot: Snapshot, compared_facts: dict[str, Any] | None = None) -> tuple[Hint, ...]:
    """No compiler, preparation, extraction, or allocation diagnostics during rendering."""
    memberships = snapshot.for_subject(subject)
    ids = {row.entity_id for row in memberships}
    providers = {row.id for row in snapshot.entities if row.kind == "provider"}
    linked = [row for row in snapshot.families if ids.intersection(row.members) and row.seed in providers]
    provider_ids = tuple(sorted({row.seed for row in linked}))
    consumer_ids = tuple(sorted({key for row in linked for key in row.members if key not in providers}))
    labels = sorted({row.primary for row in memberships if row.primary})
    state = "mixed" if any(row.state == "mixed" for row in memberships) else " / ".join(labels) or "unknown"
    mismatch_refs = tuple(sorted((compared_facts or {}).get("buckets", {})))
    methods = list(BUILTINS)
    if any(key in {"immediate", "address", "memory", "calls"} for key in mismatch_refs):
        methods.remove("types")
        methods.insert(0, "types")
    hypothesis = (
        f"{state} context: inspect retained provider declarations and their affected consumers together"
        if provider_ids
        else f"{state} context: use measured ABI, access and compare facts; semantic evidence is incomplete"
    )
    return (
        Hint(
            subject,
            snapshot.evidence_key,
            tuple(sorted({key for row in memberships for key in row.evidence_ids})),
            hypothesis,
            provider_ids,
            consumer_ids,
            tuple(methods),
            "Contradictory measured accesses or provider identity retract this hypothesis; "
            "labels never authorize types or bytes.",
            ("explain", subject, "--section", "types"),
            mismatch_refs,
        ),
    )
