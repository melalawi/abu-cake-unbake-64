"""Whole-program facts from declared ROMs, native symbols and split intervals."""

from __future__ import annotations

import hashlib
import struct
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unbake import inputs
from unbake.work import inventory as plan
from unbake.decomp.indexed import indexed_references
from unbake.layout import split
from unbake.config import Held, Project
from unbake.extract import discovered_symbols, symbols_from
from unbake.typemap import shards, storage
from unbake.typemap.mips import Analysis, control


def map_program(project: Project) -> dict[str, Any]:
    return _map(project)


def _map(project: Project, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.monotonic()
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
    old_functions: Mapping[str, dict[str, Any]] | None = None
    analyzer = inputs.digest(Path(__file__).with_name("mips.py"))
    old_rows: dict[tuple[str, int, int, int], tuple[str, str]] = {}
    old_symbols: dict[str, dict[int, list[str]]] = {v: {} for v in project.versions}
    old_targets: dict[str, dict[int, str]] = {v: {} for v in project.versions}
    if previous is not None:
        from unbake.typemap.abi_facts import refine

        source = shards.Functions(project.build / "map" / previous["shard"], previous["functions"])
        # An older analyzer's ABI supplement is materialized once. Its verified
        # facts then travel with reused bodies instead of being recomputed after
        # every symbol placement under a different instruction shard digest.
        old_functions = refine(project, {**previous, "functions": source})["functions"]
        metadata = previous["functions"]
        complete = all(
            {"start", "end", "address"} <= body.keys()
            for item in metadata.values()
            for body in item["versions"].values()
        )
        for name, item in metadata.items() if complete else old_functions.items():
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
    writer = shards.Writer(directory)
    reused = rescanned = 0
    try:
        for row in inventory:
            canonical = names[row.version, row.name]
            body = bodies[row.version, row.name]
            words = []
            prior = old_rows.get((row.version, row.start, row.end, row.address))
            analysis = None
            retained = False
            if old_functions is not None and prior == (canonical, storage.digest(body)):
                candidate = (
                    old_functions.version(canonical, row.version)
                    if isinstance(old_functions, shards.Functions)
                    else old_functions[canonical]["versions"][row.version]
                )
                accesses = {memory.get("address") for memory in candidate["memory"]}
                branches = set()
                indirect = False
                if changed_targets[row.version]:
                    words = [word for (word,) in struct.iter_unpack(">I", body)]
                    for index, word in enumerate(words):
                        branch = control(word, row.address + index * 4)
                        if branch is not None:
                            branches.add(branch[1])
                            indirect |= branch[0] in ("call", "jump") and branch[1] is None
                if not (accesses & changed_symbols[row.version] or branches & changed_targets[row.version] or indirect):
                    analysis = candidate
                    retained = True
            if analysis is None:
                words = [word for (word,) in struct.iter_unpack(">I", body)]
                analysis = Analysis(
                    canonical, row.version, row.address, row.start, words, targets[row.version], symbols[row.version]
                ).run()
                rescanned += 1
            else:
                reused += 1
            indexed = {ref.offset: ref for ref in indexed_references(words)} if not retained else {}
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
                **analysis,
                "name": row.name,
                "start": row.start,
                "end": row.end,
                "address": row.address,
                "kind": row.kind,
                "target_sha256": storage.digest(body),
            }
            writer.add(canonical, row.version, record)
            function["versions"][row.version] = {
                key: record[key] for key in ("name", "start", "end", "address", "kind", "target_sha256")
            }
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
        "format": "sqlite-zlib-v1",
        "abi_analysis_sha256": analyzer,
        "shard": shard_path.name,
        "shard_sha256": inputs.digest(shard_path),
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
            "seconds": round(time.monotonic() - started, 3),
            "abi_upgrade": previous.get("abi_analysis_sha256") != analyzer,
        }
        if not rescanned and previous.get("abi_analysis_sha256") == analyzer:
            # Metadata-only publication retains the exact instruction shard.
            if shard_path.name != previous["shard"]:
                shard_path.unlink(missing_ok=True)
            shard_path = directory / previous["shard"]
            result.update(shard=shard_path.name, shard_sha256=previous["shard_sha256"])
    if pinned != storage.map_inputs(project):
        raise Held("map", "map.inputs_stale: inputs changed during map")
    storage.write(project.build / "map/facts.json", storage.encoded(result))
    result["functions"] = shards.Functions(shard_path, functions)
    return result


def _read_map(project: Project) -> dict[str, Any]:
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
    if not shard_path.is_file() or inputs.digest(shard_path) != result.get("shard_sha256"):
        raise Held("solve", "map.shards: missing or changed facts; run unbake map")
    storage.validate_identity(project, result, "map.facts")
    return result


def load_map(project: Project, *, allow_stale: bool = False) -> dict[str, Any]:
    """Read verified shards; snapshot consumers must separately pin selected targets."""
    result = _read_map(project)
    if not allow_stale and result.get("inputs_sha256") != storage.map_inputs(project):
        raise Held("solve", "map.inputs_stale: run unbake solve to refresh affected map facts")
    result["functions"] = shards.Functions(project.build / "map" / result["shard"], result["functions"])
    return result


def refresh_map(project: Project) -> dict[str, Any]:
    """Refresh symbol and boundary dependencies, retaining unaffected instruction facts."""
    result = _read_map(project)
    pinned = storage.map_inputs(project)
    old_inputs = result["inputs_sha256"]
    for version in project.versions:
        relative = str(project.version(version).baserom.relative_to(project.root))
        if pinned.get(relative) != old_inputs.get(relative):
            raise Held("solve", f"map.rom_sha1.{version}: ROM changed; bootstrap map required")
    if pinned == old_inputs:
        result["functions"] = shards.Functions(project.build / "map" / result["shard"], result["functions"])
        return result
    return _map(project, result)


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
    pinned["config.toml"] = storage.digest(config_content)
    result["inputs_sha256"] = pinned
    result["compiler_update"] = {"config_sha256": pinned["config.toml"], "instruction_shard_retained": True}
    return path, storage.encoded(result)
