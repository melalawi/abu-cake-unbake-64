"""Whole-program facts from declared ROMs, native symbols and split intervals."""

from __future__ import annotations

import hashlib
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any

from unbake.decomp import plan
from unbake.decomp.indexed import indexed_references
from unbake.layout import split
from unbake.project.config import Held, Project
from unbake.project_tools.extract import discovered_symbols, symbols_from
from unbake.typemap import shards, storage
from unbake.typemap.mips import Analysis


def map_program(project: Project) -> dict[str, Any]:
    pinned = storage.inputs(project)
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
        rows = split.functions(project, version)
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
                for name, address in symbols_from([placements]).items():
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
    writer = shards.Writer(directory)
    try:
        for row in inventory:
            canonical = names[row.version, row.name]
            body = bodies[row.version, row.name]
            words = [word for (word,) in struct.iter_unpack(">I", body)]
            analysis = Analysis(
                canonical, row.version, row.address, row.start, words, targets[row.version], symbols[row.version]
            ).run()
            indexed = {ref.offset: ref for ref in indexed_references(words)}
            for memory in analysis["memory"]:
                relative = memory["instruction"] - row.address
                ref = indexed.get(relative)
                if ref is not None:
                    memory["indexed"] = {"anchor": ref.address, "scale": ref.scale, "extent": None}
                base = memory["base"]["constant"]
                if base is not None:
                    address = (base + memory["offset"]) & 0xFFFFFFFF
                    symbols_here = symbols[row.version].get(address, [])
                    memory["symbols"] = symbols_here
                    memory["address"] = address
                    if not symbols_here:
                        key = f"address:{row.version}:{address:08X}"
                        candidate = globals_.setdefault(
                            key, {"name": None, "versions": {row.version: {"address": address}}, "accesses": []}
                        )
                        candidate["accesses"].append(
                            {key: memory[key] for key in ("function", "version", "instruction")}
                        )
                    for symbol in symbols_here:
                        if symbol in globals_:
                            globals_[symbol]["accesses"].append(
                                {key: memory[key] for key in ("function", "version", "instruction")}
                            )
            function = functions.setdefault(canonical, {"versions": {}, "aliases": []})
            function["aliases"] = sorted(set(function["aliases"] + [row.name, *row.aliases]))
            record = {
                "name": row.name,
                "start": row.start,
                "end": row.end,
                "address": row.address,
                "kind": row.kind,
                "target_sha256": storage.digest(body),
                **analysis,
            }
            writer.add(canonical, row.version, record)
            function["versions"][row.version] = {"name": row.name, "target_sha256": record["target_sha256"]}
        shard_path = writer.finish()
    finally:
        writer.close()
    pools = {}
    for path in (project.build / "setup/layout.json", project.root / "docs/setup/layout.json"):
        if path.is_file():
            layout = storage.read(path, "map.layout")
            for version, record in layout.get("versions", {}).items():
                pools[version] = record.get("providers", [])
            break
    result = {
        **storage.identity(project),
        "inputs_sha256": pinned,
        "format": "sqlite-zlib-v1",
        "abi_analysis_sha256": storage.file_digest(Path(__file__).with_name("mips.py")),
        "shard": shard_path.name,
        "shard_sha256": storage.file_digest(shard_path),
        "functions": functions,
        "globals": globals_,
        "pools": pools,
        "unknown": [] if pools else ["map.pools: layout provider evidence is absent"],
    }
    if pinned != storage.inputs(project):
        raise Held("map", "map.inputs_stale: inputs changed during map")
    storage.write(project.build / "map/facts.json", storage.encoded(result))
    result["functions"] = shards.Functions(shard_path, functions)
    return result


def load_map(project: Project) -> dict[str, Any]:
    path = project.build / "map/facts.json"
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise Held("solve", "map.facts: compact sharded map required; run unbake map")
    result = storage.read(path, "map.facts")
    if result.get("format") != "sqlite-zlib-v1":
        raise Held("solve", "map.facts: compact sharded map required; run unbake map")
    shard = result.get("shard", "")
    if not isinstance(shard, str) or Path(shard).name != shard:
        raise Held("solve", "map.shards: invalid shard name")
    shard_path = path.parent / shard
    if not shard_path.is_file() or storage.file_digest(shard_path) != result.get("shard_sha256"):
        raise Held("solve", "map.shards: missing or changed facts; run unbake map")
    storage.validate_identity(project, result, "map.facts")
    if result.get("inputs_sha256") != storage.inputs(project):
        raise Held("solve", "map.inputs_stale: run unbake map")
    result["functions"] = shards.Functions(shard_path, result["functions"])
    return result


def refresh_map(project: Project) -> dict[str, Any]:
    """Reuse instruction facts after publication; rescan only changed boundaries."""
    path = project.build / "map/facts.json"
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise Held("solve", "map.facts: compact sharded map required; run unbake map")
    result = storage.read(path, "map.facts")
    storage.validate_identity(project, result, "map.facts")
    if result.get("format") != "sqlite-zlib-v1":
        raise Held("solve", "map.facts: compact sharded map required; run unbake map")
    shard = result.get("shard", "")
    if not isinstance(shard, str) or Path(shard).name != shard:
        raise Held("solve", "map.shards: invalid shard name")
    shard_path = path.parent / shard
    storage.verify_file(shard_path, result["shard_sha256"], "map.shards")
    pinned = storage.inputs(project)
    old_inputs = result["inputs_sha256"]
    for version in project.versions:
        relative = str(project.version(version).baserom.relative_to(project.root))
        if pinned.get(relative) != old_inputs.get(relative):
            raise Held("solve", f"map.rom_sha1.{version}: ROM changed; bootstrap map required")
    functions = shards.Functions(shard_path, result["functions"])
    current = {
        (version, row.start, row.end, row.address): row
        for version in project.versions
        for row in split.functions(project, version)
    }
    observed = set()
    metadata = {}
    for name, item in functions.items():
        versions = {}
        for version, body in item["versions"].items():
            key = version, body["start"], body["end"], body["address"]
            row = current.get(key)
            if row is None:
                return map_program(project)
            observed.add(key)
            if row.name not in item["aliases"]:
                raise Held("solve", f"map.names_changed: {name}: renamed identity needs an explicit map")
            versions[version] = {key: body[key] for key in ("start", "end", "address", "target_sha256")}
            versions[version].update(name=row.name, kind=row.kind)
        metadata[name] = {"aliases": item["aliases"], "versions": versions}
    if observed != set(current):
        return map_program(project)
    # Publication can add canonical function labels to the generated catalog.
    # New data naming requires a deliberate remap rather than retaining stale
    # symbolic origins under a newly pinned input digest.
    changed = {key for key in set(pinned) | set(old_inputs) if pinned.get(key) != old_inputs.get(key)}
    symbol_paths = {str(project.version(v).symbols.relative_to(project.root)) for v in project.versions} | {
        str((project.build_link(v) / filename).relative_to(project.root))
        for v in project.versions
        for filename in ("splat_symbols.csv", "symbol-addresses.txt")
    }
    if changed & symbol_paths:
        for version in project.versions:
            _, native = split.symbols(project.version(version).symbols)
            named = {name: address for name, (address, _, _) in native.items()}
            generated = project.build_link(version)
            table = generated / "splat_symbols.csv"
            if table.is_file():
                named = discovered_symbols(table, named)
            placements = generated / "symbol-addresses.txt"
            if placements.is_file():
                named.update(symbols_from([placements]))
            function_addresses = {key[3] for key in current if key[0] == version}
            actual = {name: address for name, address in named.items() if address not in function_addresses}
            expected = {
                name: row["versions"][version]["address"]
                for name, row in result["globals"].items()
                if row.get("name", name) is not None and version in row["versions"]
            }
            if actual != expected:
                raise Held("solve", f"map.symbols_changed: {version}: data names need an explicit map")
    pools = {}
    for layout_path in (project.build / "setup/layout.json", project.root / "docs/setup/layout.json"):
        if layout_path.is_file():
            layout = storage.read(layout_path, "map.layout")
            for version, record in layout.get("versions", {}).items():
                pools[version] = record.get("providers", [])
    result.update(
        inputs_sha256=pinned,
        functions=metadata,
        pools=pools,
        unknown=[] if pools else ["map.pools: layout provider evidence is absent"],
    )
    if pinned != storage.inputs(project):
        raise Held("solve", "map.inputs_stale: inputs changed during metadata refresh")
    storage.write(path, storage.encoded(result))
    result["functions"] = shards.Functions(shard_path, metadata)
    return result


def compiler_inputs(project: Project, config_content: bytes) -> tuple[Path, bytes] | None:
    """Rebind unchanged instruction facts to a compiler-only config publication."""
    path = project.build / "map/facts.json"
    if not path.is_file():
        return None
    result = storage.read(path, "map.facts")
    storage.validate_identity(project, result, "map.facts")
    pinned = storage.inputs(project)
    previous_inputs = result.get("inputs_sha256", {})
    changed = {key for key in set(previous_inputs) | set(pinned) if previous_inputs.get(key) != pinned.get(key)}
    if changed - {"config.toml"}:
        raise Held("setup", "map.inputs_stale: compiler update requires unchanged ROM, layout and symbol inputs")
    pinned["config.toml"] = storage.digest(config_content)
    result["inputs_sha256"] = pinned
    result["compiler_update"] = {"config_sha256": pinned["config.toml"], "instruction_shard_retained": True}
    return path, storage.encoded(result)
