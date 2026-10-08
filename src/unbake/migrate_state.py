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


def config_plan(root: Path) -> dict[str, Any]:
    """Pre-load recipe cutover. Retained evidence migration remains a separate authority."""
    import copy
    import re
    import tomllib

    import toml  # type: ignore[import-untyped]

    from unbake import config
    from unbake.compilers.config import BUILD_KEYS
    from unbake.compilers.recipe_options import (
        PHASES,
        UnitRecipe,
        canonical_unit,
        merge_options,
        partition,
        recipe_digest,
    )

    root = root.resolve()
    path = root / "config.toml"
    original = path.read_bytes()
    data = tomllib.loads(original.decode())
    planned = copy.deepcopy(data)
    blockers: list[str] = []
    unknown = sorted(set(data) - config.CONFIG_SECTIONS - {"paths"})
    blockers.extend(f"unknown section [{name}]" for name in unknown)
    project_keys = {"id", "state", "name", "title", "versions", "names_from", "default_compiler", "layout_cap"}
    blockers.extend(f"unknown [project].{name}" for name in set(data.get("project", {})) - project_keys)
    build = planned.get("build", {})
    blockers.extend(f"unknown [build].{name}" for name in set(build) - BUILD_KEYS - {"unit_cflags", "sn64_asflags"})
    if "sn64_asflags" in build:
        if "gnu_asflags" in build and build["gnu_asflags"] != build["sn64_asflags"]:
            blockers.append("[build].sn64_asflags conflicts with gnu_asflags")
        else:
            build["gnu_asflags"] = build.pop("sn64_asflags")
    paths = planned.get("paths", {})
    fixed = {
        "src": "src",
        "include": "include",
        "build": "build",
        "tools": "tools",
        "roms": "roms",
        "cache": ".unbake/cache",
        "work": "build/work",
    }
    for key, value in paths.items():
        if key not in fixed or value not in (fixed[key], [fixed[key]]):
            blockers.append(f"[paths].{key}: explicit relocation to current fixed layout required")
    if not blockers:
        planned.pop("paths", None)
    compiler_tables = planned.get("compilers", {})
    for ident, row in compiler_tables.items():
        if not isinstance(row, dict):
            blockers.append(f"[compilers].{ident}: expected table")
            continue
        blockers.extend(f"unknown [compilers.{ident}].{key}" for key in set(row) - {"cflags", "supply"})
    flags_table = build.pop("unit_cflags", {})
    units = planned.get("units", {})
    migrated: dict[str, Any] = {}
    effective: dict[str, Any] = {}
    manifest = {"config.toml": inputs.bytes_digest(original, algorithm="sha256")}
    candidates = sorted((root / "src").rglob("*.c"))

    def locate(name: str) -> str:
        if name.startswith("src/") and name.endswith(".c"):
            return canonical_unit(name)
        matches = [p.relative_to(root).as_posix() for p in candidates if p.stem == Path(name).stem]
        # ASM-backed unpublished functions also have a canonical future src path.
        if len(matches) > 1:
            raise ValueError(f"{name}: ambiguous source ownership: {matches}")
        if matches:
            return matches[0]
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise ValueError(f"{name}: unresolved source ownership")
        return f"src/{name}.c"

    # Resolve every legacy spelling FIRST, then select compiler and ordered flags
    # once per real TU. A path flag cannot invent a second default-compiler row.
    grouped: dict[str, list[tuple[str, Any]]] = {}
    flags_by_unit: dict[str, list[tuple[str, Any]]] = {}
    for table, alias_groups in ((units, grouped), (flags_table, flags_by_unit)):
        if not isinstance(table, dict):
            blockers.append("legacy units/unit_cflags must be tables")
            continue
        for name, value in table.items():
            try:
                key = locate(name)
                alias_groups.setdefault(key, []).append((name, value))
            except (ValueError, TypeError) as error:
                blockers.append(f"legacy scope {name}: {error}")
    legacy_effective: dict[str, Any] = {}
    effective_versions: dict[str, Any] = {}
    for key in sorted(set(grouped) | set(flags_by_unit)):
        try:
            declarations = []
            for _name, value in grouped.get(key, []):
                if isinstance(value, str):
                    value = {"compiler": value}
                if not isinstance(value, dict) or set(value) - {"compiler", "flags", "options", "functions"}:
                    raise ValueError("unknown unit recipe fields")
                declarations.append(value)
            ids = {row.get("compiler") for row in declarations}
            if len(ids) > 1:
                raise ValueError("conflicting canonical compiler selections")
            ident = next(iter(ids), data.get("project", {}).get("default_compiler"))
            if not isinstance(ident, str) or ident not in compiler_tables:
                raise ValueError(f"unknown compiler {ident}")
            current_rows = [row for row in declarations if "options" in row]
            if current_rows:
                if len(current_rows) != len(declarations) or flags_by_unit.get(key):
                    raise ValueError("current phase recipe conflicts with legacy flags")
                recipe = UnitRecipe.read(current_rows[0])
                if any(UnitRecipe.read(row) != recipe for row in current_rows):
                    raise ValueError("conflicting canonical recipes")
            else:
                flag_rows = sorted(flags_by_unit.get(key, []), key=lambda row: (row[0] == key, row[0]))
                raw_flags = tuple(token for _, row in flag_rows for token in row)
                inline = [tuple(row.get("flags", ())) for row in declarations]
                if len(set(inline)) > 1:
                    raise ValueError("conflicting inline flags")
                raw_flags += next(iter(inline), ())
                phases = partition(raw_flags)
                recipe = UnitRecipe(ident, tuple((p, tuple(phases[p])) for p in PHASES))
            migrated[key] = recipe.document()
            from unbake.compilers.recipe_options import resolve_options

            defaults = partition(tuple(compiler_tables[ident]["cflags"]))
            resolved = resolve_options(key, recipe, defaults)
            effective[key] = resolved.document()
            # Freeze an independently reconstructed old argv, normalized only
            # by the declared phase/group semantics (including macro order).
            raw = partition(tuple(compiler_tables[ident]["cflags"]) + (raw_flags if not current_rows else ()))
            old_phases = {p: list(merge_options(tuple(raw[p]))) for p in PHASES}
            if not current_rows and old_phases != resolved.document()["options"]:
                raise ValueError("legacy effective argv differs after migration")
            legacy_effective[key] = old_phases if not current_rows else resolved.document()["options"]
            version_effective = {}
            for version in data.get("project", {}).get("versions", ()):
                macros = tuple(data["version"][version]["macros"])
                after = resolve_options(key, recipe, defaults, version_macros=macros)
                before = {
                    p: list(
                        merge_options(
                            tuple(defaults[p]),
                            tuple("-D" + macro for macro in macros) if p == "preprocess" else (),
                            tuple(partition(raw_flags)[p]) if not current_rows else recipe.phase(p),
                            phase=p,
                        )
                    )
                    for p in PHASES
                }
                if not recipe.functions and before != after.document()["options"]:
                    raise ValueError(f"legacy version {version} effective input differs after migration")
                version_effective[version] = after.document()
            effective_versions[key] = version_effective
        except (ValueError, KeyError, TypeError) as error:
            blockers.append(f"[units].{key}: {error}")
    planned["units"] = migrated
    planned["schema"] = 2
    # Pin all persistent inputs inspected by preflight. No preprocessing/map/solve.
    visited: set[Path] = set()

    def headers(source: Path) -> None:
        if source in visited:
            return
        visited.add(source)
        if not source.is_file() or source.is_symlink():
            blockers.append(f"missing regular source/header {source.relative_to(root)}")
            return
        manifest[source.relative_to(root).as_posix()] = inputs.digest(source, algorithm="sha256", reuse=False)
        for include in re.findall(r'^\s*#\s*include\s*"([^"]+)"', source.read_text(), re.M):
            found = next((p for p in (source.parent / include, root / "include" / include) if p.is_file()), None)
            if found is None or not found.resolve().is_relative_to(root):
                blockers.append(f"{source.relative_to(root)}: missing project include {include}")
            else:
                headers(found.resolve())

    for source in candidates:
        headers(source)
    for version, row in planned.get("version", {}).items():
        for field in ("split", "symbols"):
            selected = root / row.get(field, "")
            if not selected.is_file():
                blockers.append(f"version.{version}.{field}: missing input")
            else:
                manifest[selected.relative_to(root).as_posix()] = inputs.digest(
                    selected, algorithm="sha256", reuse=False
                )
    # The offline cutover copies verified pins to immutable destinations. Old
    # compiler trees remain untouched; setup never downloads an already proved
    # ready project's binaries merely because directory identity changed.
    from unbake.compilers.registry import compiler_directory, specification, verify

    copies: dict[str, dict[str, Any]] = {}
    compiler_manifest = []
    for ident in compiler_tables:
        try:
            spec = specification(ident)
            destination = compiler_directory(root / "tools", spec)
            source = destination if destination.exists() else root / "tools" / ident
            if not source.exists():
                blockers.append(f"compiler {ident}: verified installed pins required before cutover")
                continue
            verify(source, spec)
            for name, pin in spec.pins.items():
                origin, target = source / name, destination / name
                manifest[origin.relative_to(root).as_posix()] = pin
                if origin != target:
                    copies[target.relative_to(root).as_posix()] = {
                        "source": origin.relative_to(root).as_posix(),
                        "sha256": pin,
                        "mode": origin.stat().st_mode & 0o777,
                    }
                compiler_manifest.append(f"{pin}  {target.relative_to(root).as_posix()}\n")
        except (Held, ValueError, OSError) as error:
            blockers.append(f"compiler {ident}: {error}")
    rendered = toml.dumps(planned)
    state_migration = None
    if not blockers:
        try:
            # Validate in memory against the new schema without modifying the source tree.
            staged = config.load(root, text=rendered)
            state_migration = plan(staged)
        except (Held, ValueError) as error:
            blockers.append(str(error))
    result = {
        "kind": "config.recipe-cutover",
        "schema": 2,
        "inputs": manifest,
        "writes": {"config.toml": rendered, "tools/compilers.sha256": "".join(sorted(compiler_manifest))},
        "file_copies": copies,
        "state_migration": state_migration,
        "deletes": [],
        "effective_recipes": effective,
        "effective_versions": effective_versions,
        "legacy_effective_recipes": legacy_effective,
        "blockers": sorted(set(blockers)),
        "preserved_assets": ["all source/header files", "all immutable historical/native ledger payloads"],
        "native_calls": 0,
    }
    result["plan_digest"] = recipe_digest(result)
    return result


def apply_config(root: Path, planned: dict[str, Any]) -> dict[str, Any]:
    from unbake import config
    from unbake.compilers.recipe_options import recipe_digest

    if planned.get("kind") != "config.recipe-cutover" or planned.get("blockers"):
        refuse("config plan has blockers or wrong authority")
    if recipe_digest({k: v for k, v in planned.items() if k != "plan_digest"}) != planned.get("plan_digest"):
        refuse("config plan digest differs")
    current = config_plan(root)
    if current != planned:
        refuse("config migration inputs or effective recipes changed; create a new plan")
    backup = root / ".unbake/migrations" / planned["plan_digest"]
    backup.mkdir(parents=True, exist_ok=True)
    original = (root / "config.toml").read_bytes()
    atomic.write(backup / "config.toml", original)
    atomic.write(backup / "plan.json", encoded(planned))
    with Journal(root / "build/config-migration.journal", root=root) as transaction:
        state = planned.get("state_migration") or {}
        targets = {root / name for name in (*planned["writes"], *planned["file_copies"], *state.get("inputs", {}))}
        targets.add(root / "attempts.jsonl")
        transaction.save(sorted(targets))
        for name, row in planned["file_copies"].items():
            source = root / row["source"]
            if inputs.digest(source, algorithm="sha256", reuse=False) != row["sha256"]:
                refuse("compiler source changed since cutover plan")
            target = root / name
            if target.exists():
                refuse("immutable compiler destination appeared since plan")
            atomic.write(target, source.read_bytes(), mode=row["mode"])
            if inputs.digest(target, algorithm="sha256", reuse=False) != row["sha256"]:
                refuse("compiler pin migration readback differs")
        for name, text in planned["writes"].items():
            atomic.write(root / name, text.encode())
        loaded = config.load(root)
        from unbake.compilers.recipe_options import partition, resolve_options

        observed = {
            key: resolve_options(key, recipe, partition(loaded.compilers[recipe.compiler].cflags)).document()
            for key, recipe in loaded.units.items()
        }
        if observed != planned["effective_recipes"]:
            refuse("config migration effective recipe readback differs")
        for key, versions in planned["effective_versions"].items():
            for version, expected in versions.items():
                current = resolve_options(
                    key,
                    loaded.recipe_for(key),
                    partition(loaded.compilers[loaded.compiler_reference(key)].cflags),
                    version_macros=loaded.version(version).macros,
                ).document()
                if current != expected:
                    refuse("config migration version macro/phase readback differs")
        state_result = apply(loaded, planned["state_migration"]) if planned.get("state_migration") else None
    return {
        "applied": planned["plan_digest"],
        "backup": str(backup),
        "state_migration": state_result,
        "native_calls": 0,
    }


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
    if target.exists() and inventory:
        refuse("current ledger coexists with retired state; preserve and reconcile explicitly")
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
    producer_certificates = []
    if authority is not None:
        from unbake.process import run_tool
        from unbake.report.data import producer_recipe_content
        from unbake.work.attempts import dependency_record

        archived: dict[tuple[str, str], str | None] = {}
        revisions: list[str] | None = None

        def content(path: str, sha256: str) -> str | None:
            nonlocal revisions
            key = path, sha256
            if key in archived:
                return archived[key]
            current = project.root / path
            if (
                current.is_file()
                and not current.is_symlink()
                and inputs.digest(current, algorithm="sha256", reuse=False) == sha256
            ):
                archived[key] = current.read_text()
                return archived[key]
            # A reviewed offline plan may recover a captured blob from retained
            # Git history. Hash equality, not age/head/name, establishes identity.
            if revisions is None:
                try:
                    revisions = run_tool(
                        ["git", "log", "--all", "-256", "--format=%H", "--", "Makefile"], project.root, "migration"
                    ).splitlines()
                except Held:
                    revisions = []
            for revision in revisions:
                try:
                    text = run_tool(["git", "show", revision + ":" + path], project.root, "migration")
                except Held:
                    continue
                if inputs.bytes_digest(text.encode(), algorithm="sha256") == sha256:
                    archived[key] = text
                    return text
            archived[key] = None
            return None

        for identity in history.order:
            event = history.events[identity]
            if event["kind"] != "native.data" or not event["dependencies"]["values"].get("producer_binding"):
                continue
            dependencies = dependency_record(event["dependencies"])
            pins = {pin.path.name: pin for pin in dependencies.files}
            pin = pins.get("project:Makefile")
            if pin is None or pin.state != "file" or pin.sha256 is None:
                continue
            producing_text = content("Makefile", pin.sha256)
            if producing_text is None:
                continue
            slices_name = f"versions/{dependencies.values['version']}/slices.mk"
            slices_pin = pins.get("project:" + slices_name)
            slices = (
                content(slices_name, slices_pin.sha256)
                if slices_pin and slices_pin.state == "file" and slices_pin.sha256
                else None
            )
            certificate = producer_recipe_content(
                project.name,
                dependencies.values,
                producing_text,
                pin.sha256,
                slices,
                slices_pin.sha256 if slices_pin else None,
            )
            if certificate is not None:
                producer_certificates.append(
                    {
                        "proof_event": identity,
                        "input_identity": dependencies.digest,
                        "recipe_inputs": certificate,
                        "linker_inputs": None,
                    }
                )
    return {
        "sources": sources,
        "producer_certificates": producer_certificates,
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
    config_directory = project.build / "config-migration.journal"
    if (config_directory / INDEX).is_file():
        restored = recover_journal(config_directory, root=project.root)
        return {"recovered_files": len(restored), "native_calls": 0}
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
    current = plan(project)
    if {k: v for k, v in migration.items() if k != "producer_certificates"} != {
        k: v for k, v in current.items() if k != "producer_certificates"
    }:
        refuse("migration inputs changed; create a new reviewed plan")
    target = project.root / "attempts.jsonl"
    if target.exists():
        from unbake.work.attempts import portable_tree, stored_history, validate_event

        history = Ledger(project)
        history._refresh()
        summaries, sources = history.summaries(), history.fuzzy_sources()
        original = target.read_bytes()
        normalized = []
        identities: dict[str, str] = {}
        for identity in history.order:
            old = history.events[identity]
            row = portable_tree(project, old)
            # Historical measurements remain retained but never gain a native
            # digest chain from offline migration. Native producer events stay
            # byte-identical; only legacy comparison representation is cut over.
            if row["kind"] == "compare":
                from copy import deepcopy

                row = deepcopy(row)
                value = row["result"]["value"]
                for records in (value.get("versions", {}), value.get("attempt", {}).get("versions", {})):
                    for measurement in records.values():
                        measurement.setdefault(
                            "strict",
                            {"available": False, "reason": "historical_unverified: compare again for native proof"},
                        )
                        measurement.setdefault("provenance", {})
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
            for certificate in migration.get("producer_certificates", ()):
                original_identity = certificate["proof_event"]
                proof_identity = identities[original_identity]
                value = {**certificate, "proof_event": proof_identity}
                proof = history.events[proof_identity]
                from unbake.work.attempts import dependency_record

                if dependency_record(proof["dependencies"]).digest != value["input_identity"]:
                    refuse("semantic producer certificate input binding changed")
                prior = history.latest("native.data.inputs", proof_identity)
                if prior is None or prior["result"]["value"] != value:
                    dependencies = DependencySet(
                        (),
                        {"input_identity": value["input_identity"]},
                        {"data.input_projection": inputs.digest(Path(__file__), algorithm="sha256", reuse=False)},
                    )
                    operation = Operation.make(project, "native.data.inputs", proof_identity, {}, dependencies)
                    history.record(
                        operation,
                        Outcome(
                            operation.id,
                            "ok",
                            value,
                            None,
                            {},
                            (__import__("unbake.report.data", fromlist=["digest"]).digest(value),),
                        ),
                        parents=(proof_identity,),
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
