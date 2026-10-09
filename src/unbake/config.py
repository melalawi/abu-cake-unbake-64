"""Configuration and resource loading: validated host, project and tool data, with retired keys refused by name."""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from functools import cache, partial
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from unbake.contracts import Config, Finding, Host, Json, Origin, Project, Refusal, Resident, VersionFiles, digest

SCHEMA_OF = { "keys.toml": "keys",
    "rules.toml": "rules", "units.toml": "units", "commands.toml": "commands", "retired.toml": "retired",
    "subsystems.toml": "subsystems", "toolchains.toml": "toolchains", "gbi.toml": "gbi", "hints.jsonl": "hint",
    "flow.toml": "flow", "rom.toml": "rom", "repo.toml": "repo", }
@cache
def _schema(name: str) -> Json:
    node = resources.files("unbake.resources") / "schemas" / f"{name}.schema.json"
    if not node.is_file():
        raise Refusal(Finding("config.resource", f"schema {name} is not packaged", path=f"schemas/{name}.schema.json"))
    return json.loads(node.read_text(encoding="utf-8"))
def validate(schema: str, value: Json, file: str) -> None:
    """Raise one Refusal listing every schema error of value, sorted by path."""
    errors = sorted( Draft202012Validator(_schema(schema)).iter_errors(value),
        key=lambda e: ([str(p) for p in e.absolute_path], e.message), )
    if errors:
        raise Refusal(
            *( Finding("config.schema", e.message, path=f"{file}:{'.'.join(map(str, e.absolute_path)) or '<root>'}")
                for e in errors ) )
@cache
def load_resource(name: str) -> Json:
    """Load one packaged data file, validated against its schema."""
    if name not in SCHEMA_OF:
        raise Refusal(Finding("config.resource", f"{name} is not a packaged data file", path=name))
    node = resources.files("unbake.resources") / "data" / name
    if not node.is_file():
        raise Refusal(Finding("config.resource", f"{name} is missing from the installed package", path=name))
    text = node.read_text(encoding="utf-8")
    if name.endswith(".jsonl"):
        rows = [(i, json.loads(line)) for i, line in enumerate(text.splitlines(), 1) if line.strip()]
        findings: list[Finding] = []
        for number, row in rows:
            try:
                validate(SCHEMA_OF[name], row, f"{name}:{number}")
            except Refusal as refusal:
                findings.extend(refusal.findings)
        if findings:
            raise Refusal(*findings)
        return {"rows": [row for _, row in rows]}
    document = tomllib.loads(text)
    validate(SCHEMA_OF[name], document, name)
    return document
@cache
def template(name: str) -> str:
    """Text of one packaged template; name may contain a subdirectory such as sdk/."""
    node = resources.files("unbake.resources") / "templates" / name
    if not node.is_file():
        raise Refusal(Finding("config.resource", f"template {name} is not packaged", path=f"templates/{name}"))
    return node.read_text(encoding="utf-8")
def sentence(key: str) -> str:
    return load_resource("keys.toml")["key"][key]["sentence"]
def _flatten(node: Any, prefix: str, out: dict[str, bool]) -> None:
    """Dotted key -> is_leaf for tables, arrays-of-tables rows and values."""
    if isinstance(node, dict):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            tables = isinstance(v, dict) or (isinstance(v, list) and any(isinstance(i, dict) for i in v))
            out[key] = not tables
            _flatten(v, key, out)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                _flatten(item, prefix, out)
def _retired(document: Json, file: str) -> None:
    keys: dict[str, bool] = {}
    _flatten(document, "", keys)
    findings = []
    for row in load_resource("retired.toml")["retired"]:
        if row["file"] != file:
            continue
        key = row["key"]
        if any(flat == key or flat.startswith(key + ".") for flat in keys):
            action = f"replace with {row['replacement']}" if row["replacement"] else "delete the key"
            findings.append(Finding("config.retired", row["note"], path=f"{file}:{key}", action=action))
    if findings:
        raise Refusal(*findings)
def _read(path: Path, file: str) -> tuple[dict[str, Any], str, dict[str, Origin]]:
    if not path.is_file():
        raise Refusal(Finding("config.missing", f"{path} does not exist", path=str(path), action=f"create {path}"))
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise Refusal(Finding("config.schema", f"not valid TOML: {error}", path=f"{file}:<root>")) from error
    _retired(document, file)
    validate(file, document, file)
    keys: dict[str, bool] = {}
    _flatten(document, "", keys)
    origins = {k: Origin(k, str(path), sha) for k, leaf in keys.items() if leaf}
    return document, sha, origins
def _bad(file: str, key: str, reason: str, origins: dict[str, Origin]) -> Finding:
    return Finding("config.file", reason, path=f"{file}:{key}", origin=origins.get(key))
def load_host(path: Path) -> Host:
    document, _, origins = _read(path, "host")
    problems: list[Finding] = []
    tools: dict[str, Path] = {}
    for name, text in document["tools"].items():
        tool = Path(text)
        tools[name] = tool
        key = f"tools.{name}"
        if not tool.is_absolute():
            problems.append(_bad("host", key, f"{text} is not an absolute path", origins))
        elif not tool.is_file() or not os.access(tool, os.X_OK):
            problems.append(_bad("host", key, f"{text} is not an executable file", origins))
    if not Path(document["toolchains"]["root"]).is_absolute():
        text = document["toolchains"]["root"]
        problems.append(_bad("host", "toolchains.root", f"{text} is not an absolute path", origins))
    catalog: Path | None = None
    if "sdk" in document:
        catalog = Path(document["sdk"]["catalog"])
        if not catalog.is_file():
            problems.append(_bad("host", "sdk.catalog", f"{catalog} does not exist", origins))
        else:
            try:
                validate("sdk", json.loads(catalog.read_text(encoding="utf-8")), f"sdk:{catalog}")
            except (Refusal, ValueError) as error:
                reason = "; ".join(f.reason for f in error.findings) if isinstance(error, Refusal) else str(error)
                problems.append(_bad("host", "sdk.catalog", f"{catalog} is not a valid catalog: {reason}", origins))
    res, budgets, publish = document["resources"], document["budgets"], document["publish"]
    counts: dict[str, int] = {}
    for name in ("cores", "workers"):
        key, value = f"resources.{name}", res[name]
        if value == "auto":
            counts[name] = len(os.sched_getaffinity(0))
            old = origins[key]
            origins[key] = Origin(key, old.file, old.sha256, note=f"auto: os.sched_getaffinity -> {counts[name]}")
        else:
            counts[name] = value
    if counts["workers"] > counts["cores"]:
        problems.append(_bad("host", "resources.workers", "workers exceeds cores", origins))
    if problems:
        raise Refusal(*problems)
    return Host( cores=counts["cores"], workers=counts["workers"],
        memory_parent_bytes=res["memory_parent_bytes"], memory_worker_bytes=res["memory_worker_bytes"],
        cache_max_bytes=document["cache"]["max_bytes"],
        toolchain_root=Path(document["toolchains"]["root"]), tools=tools, sdk_catalog=catalog,
        **{k: float(budgets[k]) for k in ("serial_seconds", "serial_cores", "pool_fill", "pool_fanout")},
        author=(publish["author_name"], publish["author_email"]), origins=origins, digest=digest(document))
def version_files(table: Json) -> dict[str, VersionFiles]:
    """Validated version config rows to the sole per-version path records."""
    return {v: VersionFiles(row["baserom"], row["baserom_sha1"], row["split"], row["symbols"],
                               {k: row[k] for k in ("cartridge_id", "region", "description") if k in row})
            for v, row in table.items()}
def load_project(root: Path) -> Project:
    document, _, origins = _read(root / "config.toml", "project")
    info, table = document["project"], document["version"]
    versions = tuple(info["versions"])
    problems: list[Finding] = []
    bad = partial(_bad, "project", origins=origins)
    if info["names_from"] not in versions:
        problems.append(bad("project.names_from", f"{info['names_from']} is not in project.versions"))
    if set(table) != set(versions):
        problems.append(bad("version", f"[version] keys {sorted(table)} differ from versions"))
    extra = sorted(set(document["resident"]) - set(versions))
    if extra:
        problems.append(bad("resident", f"[resident] names unknown versions {extra}"))
    if info["toolchain"] not in load_resource("toolchains.toml")["toolchain"]:
        problems.append(bad("project.toolchain", f"{info['toolchain']} is not in toolchains.toml"))
    for vid, row in table.items():
        for field in ("baserom", "split", "symbols"):
            if not (root / row[field]).is_file():
                problems.append(bad(f"version.{vid}.{field}", f"{row[field]} does not exist"))
        rom = root / row["baserom"]
        if rom.is_file():
            with rom.open("rb") as handle:
                got = hashlib.file_digest(handle, "sha1").hexdigest()
            if got != row["baserom_sha1"]:
                problems.append(bad(f"version.{vid}.baserom_sha1", f"configured {row['baserom_sha1']}, rom is {got}"))
    if problems:
        raise Refusal(*problems)
    return Project( root=root, id=info["id"], name=info["name"], title=info["title"], versions=versions,
        names_from=info["names_from"],
        toolchain=info["toolchain"], build=document["build"], version_files=version_files(table),
        version_macros={v: tuple(f"-D{m}" for m in row["macros"]) for v, row in table.items()},
        resident={v: tuple(Resident(**r) for r in document["resident"].get(v, ())) for v in versions},
        layout_cap=info["layout_cap"], origins=origins, digest=digest(document), )
def load(root: Path, host: Path) -> Config:
    project_config, host_config = load_project(root), load_host(host)
    return Config(project_config, host_config, digest((project_config.digest, host_config.digest)))
