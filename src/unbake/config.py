"""Configuration and resource loading: validated host, project and tool data."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import tomllib
from collections.abc import Callable
from functools import cache, partial
from importlib import resources
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator

from unbake.contracts import Config, Finding, Host, Json, Origin, Project, Refusal, Resident, VersionFiles, digest

SCHEMA_OF = { "keys.toml": "keys",
    "rules.toml": "rules", "units.toml": "units", "commands.toml": "commands",
    "toolchains.toml": "toolchains",
    "flow.toml": "flow", "rom.toml": "rom", "repo.toml": "repo", }
@cache
def _schema(name: str) -> Json:
    node = resources.files("unbake.resources") / "schemas" / f"{name}.schema.json"
    if not node.is_file():
        raise Refusal(Finding("config.resource", f"schema {name} is not packaged", path=f"schemas/{name}.schema.json"))
    return json.loads(node.read_text(encoding="utf-8"))
Cached = Callable[[str, str, Callable[[], bytes]], bytes]  # store.content(config).cached
def toml(schema: str, data: bytes, file: str, key: str = "config.schema",
         cache: Cached | None = None) -> Json:
    """The schema-valid document of TOML bytes; text that is not TOML is a finding under `key`. With a project, a
    document valid once is read back from its content cache: a megabyte table is parsed and
    checked once, not per run."""
    def read() -> Json:
        try:
            document = tomllib.loads(data.decode())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise Refusal(Finding(key, str(error), path=file)) from error
        validate(schema, document, file)
        return document
    if cache is None:
        return read()
    document: Json = pickle.loads(cache("toml", digest((schema, data, _schema(schema))),
                                        lambda: pickle.dumps(read(), protocol=5)))
    return document
def validate(schema: str, value: Json, file: str) -> None:
    """Raise one Refusal listing every schema error of value, sorted by path; a document of another schema version is
    one finding, not a list of its differences."""
    want = _schema(schema).get("properties", {}).get("schema", {}).get("const")
    have = value.get("schema") if isinstance(value, dict) else None
    if want is not None and have is not None and have != want:
        raise Refusal(Finding("config.schema", f"{file} is schema {have}, but this unbake reads schema {want}",
                              path=f"{file}:schema", action=f"re-create {file} as schema {want}"))
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
    document = tomllib.loads(node.read_text(encoding="utf-8"))
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
def _read(path: Path, file: str) -> tuple[dict[str, Any], str, dict[str, Origin]]:
    if not path.is_file():
        raise Refusal(Finding("config.missing", f"{path} does not exist", path=str(path), action=f"create {path}"))
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise Refusal(Finding("config.schema", f"not valid TOML: {error}", path=f"{file}:<root>")) from error
    validate(file, document, file)
    keys: dict[str, bool] = {}
    _flatten(document, "", keys)
    origins = {k: Origin(k, str(path), sha) for k, leaf in keys.items() if leaf}
    return document, sha, origins
def _bad(file: str, key: str, reason: str, origins: dict[str, Origin]) -> Finding:
    return Finding("config.file", reason, path=f"{file}:{key}", origin=origins.get(key))
def _cgroup_memory_max() -> str:
    file = Path("/sys/fs/cgroup/memory.max")
    return file.read_text(encoding="utf-8").strip() if file.is_file() else "max"
def _usable_memory() -> int:
    limit = _cgroup_memory_max()
    return int(limit) if limit.isdigit() else psutil.virtual_memory().total
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
            note = f"auto: os.sched_getaffinity -> {counts[name]}"
            if name == "workers":
                by_memory = max(1, (_usable_memory() - res["memory_parent_bytes"]) // res["memory_worker_bytes"])
                if by_memory < counts[name]:
                    note = f"auto: min({counts[name]} cpus, {by_memory} by memory) -> {by_memory}"
                    counts[name] = by_memory
            old = origins[key]
            origins[key] = Origin(key, old.file, old.sha256, note=note)
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
        problems.extend(bad(f"version.{vid}.{field}", f"{row[field]} does not exist")
                        for field in ("baserom", "split", "symbols") if not (root / row[field]).is_file())
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
