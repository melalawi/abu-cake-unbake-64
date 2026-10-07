"""Current immutable ledger fixtures, including deliberately malformed authoritative records."""

from uuid import NAMESPACE_URL, uuid5

from unbake.inputs import DependencySet
from unbake.work.attempts import Event, Outcome, encoded


def history_bytes(project, table):
    events = []
    dependencies = DependencySet((), {"dependencies_unknown": True}, {}).document()
    for name, summary in sorted(table.items()):
        value = summary.document()
        rows = [("history.imported", {"summary": value})]
        receipt = summary.fuzzy
        if receipt is not None:
            rows.append(
                (
                    "publication.fuzzy",
                    {
                        "publication": {
                            "kind": "fuzzy",
                            "source": {"root": "project", "parts": ["src", name + ".c"]},
                            "source_sha256": receipt["source_sha256"],
                            "stored_source_sha256": receipt["source_sha256"],
                            "compiler": receipt["compiler"],
                            "versions": list(receipt["versions"]),
                            "measurements": {v: None for v in receipt["versions"]},
                            "receipt": receipt,
                            "committed_source_sha256": None,
                            "origin": "history.imported",
                            "available": True,
                            "unavailable_reason": None,
                        }
                    },
                )
            )
        for kind, data in rows:
            identity = uuid5(NAMESPACE_URL, name + kind + encoded(data).decode()).hex
            outcome = Outcome(identity, "committed" if kind.startswith("publication.") else "ok", data, None, {})
            events.append(
                Event(
                    2,
                    identity,
                    identity,
                    project.id,
                    kind,
                    (),
                    name,
                    {},
                    dependencies,
                    outcome.document(),
                    {},
                    "2026-10-04T10:00:00+00:00",
                ).document()
            )
    return b"".join(encoded(row) + b"\n" for row in events)


def write_history(project, table):
    target = project.root / "attempts.jsonl"
    target.write_bytes(history_bytes(project, table))
    return target


def log_attempt(project, attempt):
    from unbake.work.attempts import ledger

    return ledger(project).compare(attempt, DependencySet((), {"dependencies_unknown": True}, {}))


def fault_evidence(fault):
    from unbake.process import Frame

    result = dict(fault.cause.evidence)
    for frame in fault.chain:
        if isinstance(frame, Frame):
            result.update(frame.evidence)
    return result


def current_receipts(ledger):
    return {
        name: dict(record.receipt)
        for name, record in ledger.publications().items()
        if record.available and record.kind == "fuzzy"
    }
