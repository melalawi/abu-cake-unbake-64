"""Explicit offline migration: preserve old evidence, verify one Ledger, then retire old files."""

from __future__ import annotations

import math
import shutil
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from unbake import atomic, inputs, strict_json
from unbake.config import Held, Project
from unbake.inputs import DependencySet, LogicalPath
from unbake.journal import Journal
from unbake.process import named
from unbake.work.attempts import Event, Ledger, Operation, Outcome, Summary, encoded, now


def refuse(reason: str) -> None:
    raise Held(named("migration.evidence", reason, owner="migrate_state", stage="migration"))


def plan(project: Project) -> dict[str, Any]:
    paths = [
        project.root / "attempts.json",
        project.build / "steps.json",
        project.build / "types/solve-input.sha256",
        *sorted(project.work.glob("*/attempts.jsonl")),
    ]
    database = project.build / "types.sqlite"
    if database.is_file():
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
        if schema == 1:
            paths.append(database)
        elif schema != 2:
            refuse(f"{database}: unknown storage schema {schema}; preserve before migration")
    inventory = {}
    for path in paths:
        if path.is_symlink():
            refuse(f"{path}: migration input is a symlink")
        if path.is_file():
            inventory[path.relative_to(project.root).as_posix()] = inputs.digest(path, algorithm="sha256", reuse=False)
    return {
        "schema": 2,
        "project_id": project.id,
        "inputs": inventory,
        "backup": ".unbake/migrations/" + inputs.bytes_digest(encoded(inventory), algorithm="sha256"),
        "counts": {"files": len(inventory), "local_logs": sum(name.endswith("/attempts.jsonl") for name in inventory)},
    }


def _summary(value: Any) -> Summary:
    if not isinstance(value, dict):
        refuse("legacy summary must be an object")
    required = {"bytes", "best", "exact", "minutes", "attempts"}
    if not required <= value.keys() or value.keys() - required - {"fuzzy"}:
        refuse("unknown legacy summary fields")
    if (
        any(type(value[k]) is not int or value[k] < 0 for k in ("bytes", "attempts"))
        or type(value["exact"]) is not bool
    ):
        refuse("legacy counters must be explicit nonnegative integers")
    if not isinstance(value["best"], dict) or any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100 for v in value["best"].values()
    ):
        refuse("invalid historical score")
    if type(value["minutes"]) not in (int, float) or not math.isfinite(value["minutes"]) or value["minutes"] < 0:
        refuse("invalid historical effort")
    return Summary(
        value["bytes"], value["best"], value["exact"], value["minutes"], value["attempts"], value.get("fuzzy")
    )


def imported(project: Project, migration: dict[str, Any]) -> tuple[bytes, dict[str, Summary]]:
    summary_path = project.root / "attempts.json"
    summaries: dict[str, Summary] = {}
    if summary_path.is_file():
        value = strict_json.read(summary_path)
        if (
            set(value) != {"v", "functions"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or not isinstance(value["functions"], dict)
        ):
            refuse("reviewed legacy attempts v1/functions shape required")
        summaries = {name: _summary(row) for name, row in value["functions"].items()}
    local = {}
    for relative in migration["inputs"]:
        if not relative.endswith("/attempts.jsonl"):
            continue
        function = Path(relative).parent.name
        rows = [
            strict_json.loads(line, relative)
            for line in (project.root / relative).read_bytes().splitlines()
            if line.strip()
        ]
        best: dict[str, float] = {}
        seconds = 0.0
        exact = False
        size = 0
        for row in rows:
            for version, result in row["versions"].items():
                percent = result.get("percent")
                if percent is not None and not result.get("fault"):
                    best[version] = max(best.get(version, 0.0), float(percent))
            seconds += row["seconds"]
            exact |= row["exact"]
            size = max(size, row["bytes"])
        local[function] = Summary(size, best, exact, seconds / 60, len(rows))
    for name in summaries.keys() | local.keys():
        old, recent = summaries.get(name, Summary(0, {}, False, 0.0, 0)), local.get(name, Summary(0, {}, False, 0.0, 0))
        count = max(old.attempts, recent.attempts)
        best = {v: max(old.best.get(v, 0), recent.best.get(v, 0)) for v in old.best.keys() | recent.best.keys()}
        summaries[name] = Summary(
            max(old.bytes, recent.bytes),
            best,
            old.exact or recent.exact,
            max(old.minutes, recent.minutes),
            count,
            old.fuzzy,
            (count, old.attempts + recent.attempts),
        )
    events = []
    dependencies = DependencySet((), {"dependencies_unknown": True, "migration": migration}, {})

    def add(
        kind: str, subject: str, value: dict[str, Any], state: Literal["ok", "blocked", "committed"] = "ok"
    ) -> None:
        op = Operation.make(project, kind, subject, {}, dependencies)
        events.append(
            Event(
                2,
                uuid.uuid4().hex,
                op.id,
                project.id,
                kind,
                (),
                subject,
                {},
                dependencies.document(),
                Outcome(op.id, state, value, None, {}).document(),
                {},
                now(),
            ).document()
        )

    for name, summary in sorted(summaries.items()):
        add(
            "history.imported",
            name,
            {"summary": summary.document(), "provenance": migration["inputs"], "proof_reusable": False},
        )
        if summary.fuzzy is not None:
            receipt = summary.fuzzy
            source = project.src / (name + ".c")
            if not source.is_file():
                refuse(f"{source}: retained fuzzy evidence missing; backup {migration['backup']}")
            digest = inputs.digest(source, algorithm="sha256", reuse=False)
            if receipt.get("source_sha256") != digest:
                refuse(f"{source}: retained source differs from receipt; backup {migration['backup']}")
            add(
                "publication.fuzzy",
                name,
                {
                    "publication": {
                        "kind": "fuzzy",
                        "source": asdict(LogicalPath("project", ("src", name + ".c"))),
                        "source_sha256": digest,
                        "stored_source_sha256": digest,
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
                "committed",
            )
    steps = project.build / "steps.json"
    if steps.is_file():
        add("history.steps", "steps", {"records": strict_json.read(steps), "proof_reusable": False})
    database = project.build / "types.sqlite"
    if database.relative_to(project.root).as_posix() in migration["inputs"]:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT function,value FROM redraft").fetchall()
        for function, value in rows:
            add("draft.required", function, {"mark": strict_json.loads(value, database)})
    return b"".join(encoded(row) + b"\n" for row in events), summaries


def apply(project: Project, migration: dict[str, Any]) -> dict[str, Any]:
    if migration != plan(project):
        refuse("migration inputs changed; create a new reviewed plan")
    target = project.root / "attempts.jsonl"
    if target.exists():
        Ledger(project)._refresh()
        if migration["inputs"]:
            refuse("current ledger coexists with retired state; preserve and reconcile explicitly")
        return {"reused": True, "events": 0}
    content, expected = imported(project, migration)
    backup = project.root / migration["backup"]
    backup.mkdir(parents=True, exist_ok=True)
    for relative in migration["inputs"]:
        original = project.root / relative
        copy = backup / relative
        if not copy.exists():
            atomic.write(copy, original.read_bytes(), mode=original.stat().st_mode & 0o777)
        if inputs.digest(copy, algorithm="sha256", reuse=False) != migration["inputs"][relative]:
            refuse(f"backup verification failed: {copy}")
    # Preserve retained headers independently of the disposable index/DB.
    for root in project.include:
        for header in root.rglob("*.h"):
            relative = header.relative_to(project.root)
            atomic.write(backup / relative, header.read_bytes(), mode=header.stat().st_mode & 0o777)
    with Journal(project.build / "migration.journal") as transaction:
        transaction.save([target, *(project.root / name for name in migration["inputs"])])
        atomic.write(target, content)
        history = Ledger(project)
        actual = history.summaries()
        if actual != expected:
            refuse(f"history readback differs; backup {backup}")
        from unbake.report import state

        state.inventory(project, receipts=history.fuzzy_sources())
        database = project.build / "types.sqlite"
        if database.relative_to(project.root).as_posix() in migration["inputs"]:
            staged = backup / "types-current.sqlite"
            shutil.copyfile(database, staged)
            with sqlite3.connect(staged) as connection:
                connection.execute("DROP TABLE redraft")
                connection.execute("PRAGMA user_version=2")
            atomic.copyfile(staged, database)
        for relative in migration["inputs"]:
            if relative != "build/types.sqlite":
                atomic.remove(project.root / relative)
    return {
        "reused": False,
        "events": len(history.order),
        "functions": len(expected),
        "backup": migration["backup"],
        "native_calls": 0,
    }
