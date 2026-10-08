"""Explicit offline migration: preserve old evidence, verify one Ledger, then retire old files."""

from __future__ import annotations

import math
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from unbake import atomic, inputs, sqlite, strict_json
from unbake.config import Held, Project
from unbake.inputs import DependencySet, LogicalPath
from unbake.journal import INDEX, Journal
from unbake.journal import recover as recover_journal
from unbake.process import named
from unbake.work.attempts import Event, Ledger, Operation, Outcome, Summary, encoded, guard_body, guard_present, now


def refuse(reason: str) -> None:
    raise Held(named("migration.evidence", reason, owner="migrate_state", stage="migration"))


def plan(project: Project) -> dict[str, Any]:
    if (project.build / "migration.journal" / INDEX).is_file():
        refuse("interrupted migration journal requires migrate-state --recover, then a new --plan")
    paths = [
        project.root / "attempts.json",
        project.build / "steps.json",
        project.build / "types/solve-input.sha256",
        *sorted(project.work.glob("*/attempts.jsonl")),
    ]
    database = project.build / "types.sqlite"
    if database.is_file():
        from unbake.typemap import types_db

        schema = types_db.legacy_storage(database)
        if schema in (0, 1):
            paths.append(database)
    inventory = {}
    for path in paths:
        if path.is_symlink():
            refuse(f"{path}: migration input is a symlink")
        if path.is_file():
            inventory[path.relative_to(project.root).as_posix()] = inputs.digest(path, algorithm="sha256", reuse=False)
    sources = {}
    for source in sorted(project.src.rglob("*.c")):
        text = source.read_text()
        if guard_present(text):
            if source.is_symlink():
                refuse(f"{source}: retained migration source is a symlink")
            guard_body(text, subject=source.relative_to(project.root).as_posix())
            sources[source.relative_to(project.root).as_posix()] = inputs.digest(
                source, algorithm="sha256", reuse=False
            )
    target = project.root / "attempts.jsonl"
    history = Ledger(project)
    authority = None
    noncanonical = 0
    if target.exists():
        history._refresh()
        from unbake.work.attempts import portable_tree

        authority = inputs.digest(target, algorithm="sha256", reuse=False)
        noncanonical = sum(
            portable_tree(project, history.events[identity]) != history.events[identity] for identity in history.order
        )
    backup_inputs = {**inventory, **({"attempts.jsonl": authority} if authority is not None else {})}
    return {
        "sources": sources,
        "schema": 2,
        "project_id": project.id,
        "inputs": inventory,
        "ledger": authority,
        "noncanonical_events": noncanonical,
        "backup": ".unbake/migrations/" + inputs.bytes_digest(encoded(backup_inputs), algorithm="sha256"),
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


def retain(
    project: Project, migration: dict[str, Any], receipts: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Bind existing opt-in sources to actual ASM membership, with no native proof."""
    from unbake.layout import split

    missing = {
        Path(path).relative_to("src").with_suffix("").as_posix(): digest
        for path, digest in migration["sources"].items()
        if Path(path).relative_to("src").with_suffix("").as_posix() not in receipts
    }
    if not missing:
        return {}
    holdings: dict[str, list[str]] = {name: [] for name in missing}
    for version in project.versions:
        for row in split.functions(project, version):
            for member in split.unit_members(row):
                for alias in member.aliases:
                    if alias in holdings and version not in holdings[alias]:
                        holdings[alias].append(version)
    return {
        name: {
            "source_sha256": digest,
            "compiler": project.compiler_reference(name),
            "versions": holdings[name],
            "verification": "unverified",
        }
        for name, digest in missing.items()
    }


def recover(project: Project) -> dict[str, Any]:
    """Public recovery uses the migration's own before-images, never an older writer."""
    directory = project.build / "migration.journal"
    index = directory / INDEX
    if not index.is_file():
        return {"recovered_files": 0, "native_calls": 0}
    record = strict_json.read(index)
    if isinstance(record, dict) and record.get("schema") == 2:
        restored = recover_journal(directory, root=project.root)
        return {"recovered_files": len(restored), "native_calls": 0}
    # 1cb wrote an array of exactly path/backup rows. This offline boundary is
    # the only retired journal reader; ordinary Journal refuses it.
    if not isinstance(record, list) or any(
        not isinstance(row, dict) or set(row) != {"path", "backup"} for row in record
    ):
        refuse("unknown old migration journal shape; preserve before-images")
    rows = []
    for row in record:
        path = Path(row["path"])
        backup = directory / row["backup"] if row["backup"] is not None else None
        if (
            not path.is_absolute()
            or not path.is_relative_to(project.root)
            or path.is_symlink()
            or any(p.is_symlink() for p in path.parents)
        ):
            refuse("old migration output escapes owned regular path")
        if backup is not None and (backup.parent != directory or not backup.is_file() or backup.is_symlink()):
            refuse("old migration before-image unavailable")
        mode: int | None
        if backup is not None and not path.exists():
            # The original migration's separately verified backup preserves modes.
            digest = inputs.digest(backup, algorithm="sha256", reuse=False)
            relative = path.relative_to(project.root)
            candidates = (
                [p / relative for p in (project.root / ".unbake/migrations").iterdir()]
                if (project.root / ".unbake/migrations").is_dir()
                else []
            )
            evidence = next(
                (
                    p
                    for p in candidates
                    if p.is_file()
                    and not p.is_symlink()
                    and inputs.digest(p, algorithm="sha256", reuse=False) == digest
                ),
                None,
            )
            if evidence is None:
                refuse("old journal lacks original file mode; preserve journal and migration backup for review")
            assert evidence is not None
            mode = evidence.stat().st_mode & 0o777
        else:
            mode = path.stat().st_mode & 0o777 if path.is_file() else None
        rows.append((path, backup, mode))
    for path, backup, mode in reversed(rows):
        if backup is None:
            if path.exists():
                atomic.remove(path)
        else:
            content = backup.read_bytes()
            atomic.write(path, content, mode=mode)
            if path.read_bytes() != content:
                refuse("old migration recovery readback differs")
    archive = directory.with_name(directory.name + ".archive")
    archive.mkdir(parents=True, exist_ok=True)
    import os

    os.replace(directory, archive / ("legacy-" + uuid.uuid4().hex))
    atomic.sync_directory(archive)
    atomic.sync_directory(directory.parent)
    return {"recovered_files": len(rows), "native_calls": 0}


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
        stable = uuid.uuid5(uuid.NAMESPACE_URL, project.id + kind + subject + encoded(value).decode()).hex
        op = Operation(stable, project.id, kind, subject, {}, dependencies)
        events.append(
            Event(
                2,
                stable,
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
    receipts = {name: summary.fuzzy for name, summary in summaries.items() if summary.fuzzy is not None}
    for name, row in retain(project, migration, receipts).items():
        add("source.retained", name, {"retained": row, "provenance": migration["sources"]})
    steps = project.build / "steps.json"
    if steps.is_file():
        add("history.steps", "steps", {"records": strict_json.read(steps), "proof_reusable": False})
    database = project.build / "types.sqlite"
    if database.relative_to(project.root).as_posix() in migration["inputs"]:
        with closing(sqlite.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            rows = connection.execute("SELECT function,value FROM redraft").fetchall()
        for function, value in rows:
            add("draft.required", function, {"mark": strict_json.loads(value, database)})
    return b"".join(encoded(row) + b"\n" for row in events), summaries


def apply(project: Project, migration: dict[str, Any]) -> dict[str, Any]:
    if migration != plan(project):
        refuse("migration inputs changed; create a new reviewed plan")
    target = project.root / "attempts.jsonl"
    if target.exists():
        from unbake.work.attempts import portable_tree, stored_history, validate_event

        history = Ledger(project)
        history._refresh()
        if migration["inputs"]:
            refuse("current ledger coexists with retired state; preserve and reconcile explicitly")
        summaries, sources = history.summaries(), history.fuzzy_sources()
        original = target.read_bytes()
        normalized = []
        identities: dict[str, str] = {}
        for identity in history.order:
            old = history.events[identity]
            row = portable_tree(project, old)
            parents = [identities[parent] for parent in old["parents"]]
            if row != old or parents != old["parents"]:
                row["event_id"] = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "current-ledger:" + identity + ":" + inputs.bytes_digest(encoded(old), algorithm="sha256"),
                ).hex
                row["parents"] = parents
                row["request"]["representation_origin"] = {
                    "event_id": identity,
                    "payload_sha256": inputs.bytes_digest(encoded(old), algorithm="sha256"),
                }
            identities[identity] = row["event_id"]
            validate_event(row)
            normalized.append(row)
        changed = sum(identities[i] != i for i in identities)
        if changed:
            backup = project.root / migration["backup"] / "attempts.jsonl"
            if not backup.exists():
                atomic.write(backup, original)
            if inputs.digest(backup, algorithm="sha256", reuse=False) != migration["ledger"]:
                refuse("current-ledger backup verification failed; preserve original")
        with Journal(project.build / "migration.journal", root=project.root) as transaction:
            transaction.save([target])
            if changed:
                atomic.write(target, stored_history(project, normalized))
                history = Ledger(project)
                if (
                    history.summaries() != summaries
                    or history.fuzzy_sources() != sources
                    or len(history.order) != len(normalized)
                ):
                    refuse("current-ledger representation readback differs; verified original backup retained")
            known = dict(history.fuzzy_sources())
            known.update({name: {} for name in history.publications()})
            retained = retain(project, migration, known)
            for name, row in retained.items():
                history.note(
                    "source.retained",
                    name,
                    {"retained": row, "provenance": migration["sources"]},
                    dependencies=DependencySet((), {"migration": migration, "dependencies_unknown": True}, {}),
                )
            from unbake.report import state

            state.inventory(project, receipts=history.fuzzy_sources())
        if changed:
            return {
                "reused": False,
                "events": len(normalized),
                "changed": changed,
                "backup": migration["backup"],
                "native_calls": 0,
            }
        return {"reused": True, "events": len(retained)}
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
    with Journal(project.build / "migration.journal", root=project.root) as transaction:
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
            atomic.copyfile(database, staged)
            with closing(sqlite.connect(staged)) as connection, connection:
                connection.execute("DROP TABLE redraft")
                connection.execute("PRAGMA user_version=2")
                metadata = {key for (key,) in connection.execute("SELECT key FROM meta")}
                from unbake.typemap import types_db

                if not set(types_db.REUSE_META) <= metadata:
                    connection.execute("INSERT INTO meta VALUES (?,?)", ("migration_reuse", '"unverified"'))
            from unbake.typemap import types_db

            if types_db.legacy_storage(staged) != types_db.DB_SCHEMA:
                refuse("converted type storage readback failed")
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
