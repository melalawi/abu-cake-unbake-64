"""Whole-program facts from declared ROMs, native symbols and split intervals."""

from __future__ import annotations

import hashlib
import struct
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unbake import cache as retention
from unbake import inputs, pool, tui
from unbake.config import Held, Host, Project
from unbake.decomp.indexed import indexed_references
from unbake.extract import discovered_symbols, read_symbol_table
from unbake.layout import split
from unbake.typemap import shards, storage
from unbake.typemap.mips import Analysis, control
from unbake.work import inventory as plan

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 5


def _analysis(
    shared: dict[str, tuple[dict[int, str], dict[int, list[str]]]], row: tuple[str, str, int, int, bytes]
) -> Any:
    """Pool worker: the instruction analysis of one function; SHARED holds each version's targets and symbols."""
    canonical, version, address, start, body = row
    targets, symbols = shared[version]
    return Analysis(
        canonical, version, address, start, [word for (word,) in struct.iter_unpack(">I", body)], targets, symbols
    ).run()


@pool.cpu
def _mapped(shared: Any, job: Any) -> Any:
    """Read, annotate and compress one affected body in a worker, never in the parent."""
    targets, symbols, old, changed_symbols, changed_targets = shared
    row, canonical, binary, prior = job
    words = [word for (word,) in struct.iter_unpack(">I", binary)]
    dependencies: dict[str, Any] = {"symbols": [], "targets": [], "indirect": False}
    branches = set()
    for index, word in enumerate(words):
        branch = control(word, row.address + index * 4)
        if branch is not None:
            branches.add(branch[1])
            dependencies["indirect"] |= branch[0] in ("call", "jump") and branch[1] is None
    dependencies["targets"] = sorted(at for at in branches if at is not None)
    retained = False
    if prior and old is not None:
        candidate = (
            old.version(canonical, row.version)
            if isinstance(old, shards.Functions)
            else old[canonical]["versions"][row.version]
        )
        addresses = {memory.get("address") for memory in candidate["memory"]}
        retained = not (
            addresses & changed_symbols[row.version]
            or branches & changed_targets[row.version]
            or (dependencies["indirect"] and changed_targets[row.version])
        )
    analysis = (
        candidate
        if retained
        else _analysis(
            {v: (targets[v], symbols[v]) for v in targets}, (canonical, row.version, row.address, row.start, binary)
        )
    )
    indexed = {ref.offset: ref for ref in indexed_references(words)} if not retained else {}
    accesses = []
    for memory in analysis["memory"]:
        ref = indexed.get(memory["instruction"] - row.address)
        if ref is not None:
            memory["indexed"] = {"anchor": ref.address, "scale": ref.scale, "extent": None}
        base = memory["base"]["constant"]
        if base is not None:
            address = (base + memory["offset"]) & 0xFFFFFFFF
            memory["symbols"] = symbols[row.version].get(address, [])
            memory["address"] = address
            accesses.append([address, memory["instruction"]])
    dependencies["symbols"] = sorted({at for at, _ in accesses})
    metadata = {
        "name": row.name,
        "start": row.start,
        "end": row.end,
        "address": row.address,
        "kind": row.kind,
        "target_sha256": inputs.bytes_digest(binary, algorithm="sha256"),
        "_refresh": {**dependencies, "accesses": accesses},
    }
    return metadata, None if retained and isinstance(old, shards.Functions) else shards.pack(
        {**analysis, **{k: v for k, v in metadata.items() if k != "_refresh"}}
    )


@pool.cpu
def _placements(functions: Any, name: str) -> tuple[str, dict[str, Any]]:
    item = functions[name]
    return name, {
        "versions": {
            version: {key: body[key] for key in ("start", "end", "address", "target_sha256")}
            for version, body in item["versions"].items()
        }
    }


def map_program(project: Project, host: Host) -> dict[str, Any]:
    with pool.session(host), tui.task("Indexing ROM intervals and symbols"):
        return _map(project, host)


def _map(project: Project, host: Host, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    pinned = storage.map_inputs(project)
    images = {}
    inventory = []
    symbols: dict[str, dict[int, list[str]]] = {}
    for version in project.versions:
        cartridge = project.version(version)
        try:
            image = cartridge.baserom.read_bytes()
        except OSError as error:
            raise Held("map", f"map.rom_sha1.{version}: {error}") from error
        if hashlib.sha1(image).hexdigest() != cartridge.baserom_sha1:
            raise Held("map", f"map.rom_sha1.{version}: ROM differs from confirmed digest")
        images[version] = image
        rows = split.members(project, version)
        if not rows:
            raise Held("map", f"map.functions.{version}: no declared function intervals")
        inventory.extend(rows)
        _, native = split.symbols(cartridge.symbols)
        named = {name: address for name, (address, _, _) in native.items()}
        generated = project.build_link(version)
        try:
            table = generated / "splat_symbols.csv"
            if table.is_file():
                named = discovered_symbols(table, named)
            placements = generated / "symbol-addresses.txt"
            if placements.is_file():
                for name, address in read_symbol_table(placements).items():
                    if name in named and named[name] != address:
                        raise ValueError(f"conflicting generated symbol {name}")
                    named[name] = address
        except (OSError, ValueError, KeyError) as error:
            raise Held("map", f"map.symbols.{version}: {error}") from error
        by_address: dict[int, list[str]] = defaultdict(list)
        for name, address in named.items():
            by_address[address].append(name)
        symbols[version] = dict(by_address)
    bodies = {}
    for row in inventory:
        image = images[row.version]
        body = image[row.start : row.end]
        if row.start < 0 or row.end > len(image) or not body or len(body) % 4:
            raise Held("map", f"map.functions.{row.version}: {row.name}: invalid ROM word interval")
        bodies[row.version, row.name] = body
    groups = plan.groups(inventory, bodies)
    names: dict[tuple[str, str], str] = {}
    targets: dict[str, dict[int, str]] = {v: {} for v in project.versions}
    for group in groups:
        ordered = sorted(group, key=lambda row: project.versions.index(row.version))
        canonical = ordered[0].name
        for row in group:
            names[row.version, row.name] = canonical
            if row.address in targets[row.version]:
                raise Held("map", f"map.functions.{row.version}: duplicate runtime address 0x{row.address:X}")
            targets[row.version][row.address] = canonical
    old_functions: Mapping[str, dict[str, Any]] | None = None
    analyzer = inputs.digest(Path(__file__).with_name("mips.py"), algorithm="sha256", reuse=retention.configured())
    old_rows: dict[tuple[str, int, int, int], tuple[str, str]] = {}
    old_symbols: dict[str, dict[int, list[str]]] = {v: {} for v in project.versions}
    old_targets: dict[str, dict[int, str]] = {v: {} for v in project.versions}
    if previous is not None:
        from unbake.typemap.abi_facts import refine

        source = shards.Functions(project.build / "map" / previous["shard"], previous["functions"])
        # An older analyzer's ABI supplement is materialized once. Its verified
        # facts then travel with reused bodies instead of being recomputed after
        # every symbol placement under a different instruction shard digest.
        old_functions = refine(project, {**previous, "functions": source}, host)["functions"]
        metadata = previous["functions"]
        complete = all(
            {"start", "end", "address"} <= body.keys()
            for item in metadata.values()
            for body in item["versions"].values()
        )
        if not complete:
            with tui.task("Upgrading assembly interval metadata", len(metadata)):
                metadata = dict(pool.run(host, _placements, list(metadata), old_functions))
        for name, item in metadata.items():
            for version, record in item["versions"].items():
                interval_key = version, record["start"], record["end"], record["address"]
                old_rows[interval_key] = name, record["target_sha256"]
                old_targets[version][record["address"]] = name
        if "symbols" in previous:
            old_symbols = {
                v: {int(at): names for at, names in table.items()} for v, table in previous["symbols"].items()
            }
        for name, record in () if "symbols" in previous else previous["globals"].items():
            if record.get("name", name) is not None:
                for version, placement in record["versions"].items():
                    old_symbols[version].setdefault(placement["address"], []).append(name)
    changed_symbols = {
        v: {
            address
            for address in set(old_symbols[v]) | set(symbols[v])
            if sorted(old_symbols[v].get(address, [])) != sorted(symbols[v].get(address, []))
        }
        for v in project.versions
    }
    changed_targets = {
        v: {
            address
            for address in set(old_targets[v]) | set(targets[v])
            if old_targets[v].get(address) != targets[v].get(address)
        }
        for v in project.versions
    }
    functions: dict[str, Any] = {}
    globals_: dict[str, Any] = {}
    for version in project.versions:
        for address, symbol_names in symbols[version].items():
            if address in targets[version]:
                continue
            for symbol in symbol_names:
                record = globals_.setdefault(symbol, {"versions": {}, "accesses": []})
                record["versions"][version] = {"address": address}
    directory = project.build / "map"
    directory.mkdir(parents=True, exist_ok=True)
    jobs = []
    retained_rows = []
    with tui.task("Selecting changed assembly facts", len(inventory)):
        for row in inventory:
            canonical = names[row.version, row.name]
            binary = bodies[row.version, row.name]
            prior = old_rows.get((row.version, row.start, row.end, row.address))
            same = old_functions is not None and prior == (canonical, inputs.bytes_digest(binary, algorithm="sha256"))
            metadata = (
                previous["functions"].get(canonical, {}).get("versions", {}).get(row.version, {}) if previous else {}
            )
            deps = metadata.get("_refresh")
            affected = deps is None or bool(
                set(deps["symbols"]) & changed_symbols[row.version]
                or set(deps["targets"]) & changed_targets[row.version]
                or (deps["indirect"] and changed_targets[row.version])
            )
            if same and not affected:
                retained_rows.append((row, canonical, {**metadata, "name": row.name, "kind": row.kind}, None))
            else:
                jobs.append((row, canonical, binary, same))
    with tui.task("Reading changed assembly bodies", len(jobs)):
        shared = targets, symbols, old_functions, changed_symbols, changed_targets
        found = pool.run(host, _mapped, jobs, shared)
    packed = []
    rescanned = 0
    with tui.task("Updating assembly ownership and global uses", len(inventory)):
        completed = retained_rows + [
            (row, canonical, metadata, body)
            for (row, canonical, _, _), (metadata, body) in zip(jobs, found, strict=True)
        ]
        for row, canonical, metadata, body in completed:
            function = functions.setdefault(canonical, {"versions": {}, "aliases": []})
            function["aliases"] = sorted(set(function["aliases"] + [row.name, *row.aliases]))
            function["versions"][row.version] = metadata
            if body is not None:
                rescanned += 1
                packed.append((canonical, row.version, body))
            for address, instruction in metadata["_refresh"]["accesses"]:
                symbols_here = symbols[row.version].get(address, [])
                access = {"function": canonical, "version": row.version, "instruction": instruction}
                if not symbols_here:
                    key = f"address:{row.version}:{address:08X}"
                    candidate = globals_.setdefault(
                        key, {"name": None, "versions": {row.version: {"address": address}}, "accesses": []}
                    )
                    candidate["accesses"].append(access)
                for symbol in symbols_here:
                    if symbol in globals_:
                        globals_[symbol]["accesses"].append(access)
    for record in globals_.values():
        record["accesses"].sort(key=lambda access: (access["version"], access["function"], access["instruction"]))
    reused = len(inventory) - rescanned
    with tui.task("Saving assembly facts", len(packed)):
        if previous is not None and not rescanned and previous.get("abi_analysis_sha256") == analyzer:
            shard_path = directory / previous["shard"]
        else:
            writer = shards.Writer(directory)
            try:
                if previous is not None and previous.get("abi_analysis_sha256") == analyzer:
                    writer.copy(
                        directory / previous["shard"],
                        [(name, version) for name, item in functions.items() for version in item["versions"]],
                    )
                for canonical, version, body in packed:
                    writer.add_packed(canonical, version, body)
                shard_path = writer.finish()
            finally:
                writer.close()
    pools = {}
    for path in (project.build / "setup/layout.json",):
        if path.is_file():
            layout = storage.read(path, "map.layout")
            for version, record in layout.get("versions", {}).items():
                pools[version] = record.get("providers", [])
            break
    result = {
        **storage.identity(project),
        "inputs_sha256": pinned,
        "map_schema": SCHEMA,
        "format": "sqlite-zlib-v1",
        "abi_analysis_sha256": analyzer,
        "shard": shard_path.name,
        "shard_sha256": inputs.digest(shard_path, algorithm="sha256", reuse=retention.configured()),
        "functions": functions,
        "symbols": symbols,
        "globals": globals_,
        "pools": pools,
        "unknown": [] if pools else ["map.pools: layout provider evidence is absent"],
    }
    if previous is not None:
        result["refresh"] = {
            "reused": reused,
            "rescanned": rescanned,
            "previous_shard_sha256": previous["shard_sha256"],
            "abi_upgrade": previous.get("abi_analysis_sha256") != analyzer,
        }
    if pinned != storage.map_inputs(project):
        raise Held("map", "map.inputs_stale: inputs changed during map")
    storage.write(project.build / "map/facts.json", storage.encoded(result))
    result["functions"] = shards.Functions(shard_path, functions)
    return result


def _read_map(project: Project) -> dict[str, Any]:
    path = project.build / "map/facts.json"
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise Held("solve", "map.facts: compact sharded map required; run unbake recompute rom-facts")
    result = storage.read(path, "map.facts")
    if result.get("format") != "sqlite-zlib-v1":
        raise Held("solve", "map.facts: compact sharded map required; run unbake recompute rom-facts")
    shard = result.get("shard", "")
    if not isinstance(shard, str) or Path(shard).name != shard:
        raise Held("solve", "map.shards: invalid shard name")
    shard_path = path.parent / shard
    if not shard_path.is_file() or inputs.digest(
        shard_path, algorithm="sha256", reuse=retention.configured()
    ) != result.get("shard_sha256"):
        raise Held("solve", "map.shards: missing or changed facts; run unbake recompute rom-facts")
    storage.validate_identity(project, result, "map.facts")
    return result


def load_map(project: Project, *, allow_stale: bool = False) -> dict[str, Any]:
    """Read verified shards; snapshot consumers must separately pin selected targets."""
    result = _read_map(project)
    if not allow_stale and result.get("inputs_sha256") != storage.map_inputs(project):
        raise Held("solve", "map.inputs_stale: run unbake recompute rom-facts")
    result["functions"] = shards.Functions(project.build / "map" / result["shard"], result["functions"])
    return result


def refresh_map(project: Project, host: Host | None) -> dict[str, Any]:
    """Refresh symbol and boundary dependencies, retaining unaffected instruction facts."""
    result = _read_map(project)
    pinned = storage.map_inputs(project)
    old_inputs = result["inputs_sha256"]
    for version in project.versions:
        relative = str(project.version(version).baserom.relative_to(project.root))
        if pinned.get(relative) != old_inputs.get(relative):
            raise Held("solve", f"map.rom_sha1.{version}: ROM changed; bootstrap map required")
    # A map an older schema wrote is rebuilt; its unchanged instruction facts are reused by interval.
    if pinned == old_inputs and result.get("map_schema") == SCHEMA:
        result["functions"] = shards.Functions(project.build / "map" / result["shard"], result["functions"])
        return result
    if host is None:
        raise Held("map", "map.host: the map is stale and rebuilding it needs the host's worker pool")
    with pool.session(host), tui.task("Indexing ROM intervals and symbols"):
        return _map(project, host, result)


def compiler_inputs(project: Project, config_content: bytes) -> tuple[Path, bytes] | None:
    """Rebind unchanged instruction facts to a compiler-only config publication."""
    path = project.build / "map/facts.json"
    if not path.is_file():
        return None
    result = storage.read(path, "map.facts")
    storage.validate_identity(project, result, "map.facts")
    pinned = storage.map_inputs(project)
    previous_inputs = result.get("inputs_sha256", {})
    changed = {key for key in set(previous_inputs) | set(pinned) if previous_inputs.get(key) != pinned.get(key)}
    if changed - {"config.toml"}:
        raise Held("setup", "map.inputs_stale: compiler update requires unchanged ROM, layout and symbol inputs")
    pinned["config.toml"] = inputs.bytes_digest(config_content, algorithm="sha256")
    result["inputs_sha256"] = pinned
    result["compiler_update"] = {"config_sha256": pinned["config.toml"], "instruction_shard_retained": True}
    return path, storage.encoded(result)
