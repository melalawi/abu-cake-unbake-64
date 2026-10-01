"""Whole-program facts from declared ROMs, native symbols and split intervals."""

from __future__ import annotations

import hashlib
import struct
from collections import defaultdict
from typing import Any

from unbake.decomp import plan
from unbake.decomp.indexed import indexed_references
from unbake.layout import split
from unbake.project.config import Held, Project
from unbake.typemap import storage
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
        by_address: dict[int, list[str]] = defaultdict(list)
        for name, (address, _, _) in native.items():
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
                    candidate["accesses"].append(memory)
                for symbol in symbols_here:
                    if symbol in globals_:
                        globals_[symbol]["accesses"].append(memory)
        function = functions.setdefault(canonical, {"versions": {}, "aliases": []})
        function["aliases"] = sorted(set(function["aliases"] + [row.name, *row.aliases]))
        function["versions"][row.version] = {
            "name": row.name,
            "start": row.start,
            "end": row.end,
            "address": row.address,
            "kind": row.kind,
            "target_sha256": storage.digest(body),
            **analysis,
        }
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
        "functions": functions,
        "globals": globals_,
        "pools": pools,
        "unknown": [] if pools else ["map.pools: layout provider evidence is absent"],
    }
    if pinned != storage.inputs(project):
        raise Held("map", "map.inputs_stale: inputs changed during map")
    storage.write(project.build / "map/facts.json", storage.encoded(result))
    return result


def load_map(project: Project) -> dict[str, Any]:
    path = project.build / "map/facts.json"
    result = storage.read(path, "map.facts")
    storage.validate_identity(project, result, "map.facts")
    if result.get("inputs_sha256") != storage.inputs(project):
        raise Held("solve", "map.inputs_stale: run unbake map")
    return result
