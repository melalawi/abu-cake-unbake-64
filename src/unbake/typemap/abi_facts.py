"""Refine register definedness without replacing the mapped storage facts."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from unbake import cache as retention
from unbake import inputs, pool, tui
from unbake.config import Held, Host, Project
from unbake.typemap import jump_tables, shards, storage
from unbake.typemap.mips import Analysis


@pool.cpu
def _refined(shared: Any, job: Any) -> bytes:
    project, targets, symbols = shared
    name, version, body = job
    with project.version(version).baserom.open("rb") as stream:
        stream.seek(body["start"])
        binary = stream.read(body["end"] - body["start"])
    if hashlib.sha256(binary).hexdigest() != body["target_sha256"]:
        raise Held("solve", f"map.abi.target_sha256: {version}: {name}: ROM target changed")
    words = [word for (word,) in struct.iter_unpack(">I", binary)]
    edges = {}
    if any(word >> 26 == 0 and word & 63 == 8 and (word >> 21 & 31) != 31 for word in words):
        from unbake.decomp.rom import project_reader

        edges = jump_tables.targets(words, body["address"], project_reader(project, version))
    result = Analysis(
        name, version, body["address"], body["start"], words, targets[version], symbols[version], jump_targets=edges
    ).run()
    return shards.pack(
        {
            "register_inputs": result["register_inputs"],
            "register_outputs": result["register_outputs"],
            "value_types": result["value_types"],
            "memory": {str(row["instruction"]): row for row in result["memory"]},
            "calls": result["calls"],
            "returns": result["returns"],
            "unknown": result["unknown"],
        }
    )


def refine(project: Project, facts: dict[str, Any], policy: Host | None = None) -> dict[str, Any]:
    """Keep the original map shard; pin a separate ABI evidence supplement."""
    analyzer = inputs.bytes_digest(
        storage.encoded(
            [
                inputs.digest(path, algorithm="sha256", reuse=retention.configured())
                for path in (Path(__file__), Path(__file__).with_name("mips.py"), Path(jump_tables.__file__))
            ]
        ),
        algorithm="sha256",
    )
    roms = {
        version: inputs.digest(project.version(version).baserom, algorithm="sha256", reuse=retention.configured())
        for version in project.versions
    }
    if facts.get("abi_analysis_sha256") == analyzer and facts.get("abi_rom_sha256") == roms:
        return facts
    key = inputs.bytes_digest(
        (
            facts["shard_sha256"]
            + analyzer
            + inputs.digest(Path(__file__), algorithm="sha256", reuse=retention.configured())
            + json.dumps(roms, sort_keys=True)
            + json.dumps(
                {version: [vars(row) for row in rows] for version, rows in project.resident_mappings.items()},
                sort_keys=True,
            )
        ).encode(),
        algorithm="sha256",
    )
    index = project.build / "map" / ("abi-index-" + key + ".json")
    functions = facts["functions"]
    metadata = {
        name: {"aliases": [], "versions": {version: {} for version in item["versions"]}}
        for name, item in getattr(functions, "inventory", functions).items()
    }
    if index.is_file():
        pointer = storage.read(index, "map.abi")
        filename = pointer.get("path")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise Held("solve", "map.abi.path: invalid ABI supplement name")
        path = index.parent / filename
        storage.verify_file(path, pointer["sha256"], "map.abi")
        storage.validate_identity(project, pointer, "map.abi")
        if pointer.get("map_shard_sha256") != facts["shard_sha256"]:
            raise Held("solve", "map.abi.shard: ABI supplement belongs to another map shard")
    else:
        inventory = {
            name: {
                "versions": {
                    version: {key: body[key] for key in ("address", "start", "end", "target_sha256")}
                    for version, body in item["versions"].items()
                }
            }
            for name, item in getattr(functions, "inventory", functions).items()
        }
        targets = {
            version: {
                body["address"]: name
                for name, item in inventory.items()
                for body in [item["versions"].get(version)]
                if body
            }
            for version in project.versions
        }
        symbols = {
            version: {
                row["versions"][version]["address"]: [name]
                for name, row in facts["globals"].items()
                if version in row["versions"]
            }
            for version in project.versions
        }
        writer = shards.Writer(index.parent)
        try:
            jobs = [
                (name, version, body) for name, item in inventory.items() for version, body in item["versions"].items()
            ]
            shared = project, targets, symbols
            with tui.task("Upgrading assembly ABI facts", len(jobs)):
                rows = (
                    [_refined(shared, job) for job in jobs]
                    if policy is None
                    else pool.run(policy, _refined, jobs, shared)
                )
            for (name, version, _), body in zip(jobs, rows, strict=True):
                writer.add_packed(name, version, body)
            path = writer.finish()
        finally:
            writer.close()
        storage.write(
            index,
            storage.encoded(
                {
                    **storage.identity(project),
                    "map_shard_sha256": facts["shard_sha256"],
                    "path": path.name,
                    "sha256": inputs.digest(path, algorithm="sha256", reuse=retention.configured()),
                }
            ),
        )
    return {
        **facts,
        "abi_analysis_sha256": analyzer,
        "abi_rom_sha256": roms,
        "abi_supplement": {
            "path": path.name,
            "sha256": inputs.digest(path, algorithm="sha256", reuse=retention.configured()),
        },
        "functions": Functions(functions, shards.Functions(path, metadata)),
    }


class Functions(Mapping[str, dict[str, Any]]):
    """Overlay compact ABI observations during bounded map iteration."""

    def __init__(self, source: Mapping[str, dict[str, Any]], supplement: Mapping[str, dict[str, Any]]) -> None:
        self.source, self.supplement = source, supplement
        self.inventory = getattr(source, "inventory", source)

    def __len__(self) -> int:
        return len(self.source)

    def __iter__(self) -> Iterator[str]:
        return iter(self.source)

    def __getitem__(self, name: str) -> dict[str, Any]:
        item = self.source[name]
        records = self.supplement[name]["versions"]
        for version, body in item["versions"].items():
            record = records[version]
            for key in ("register_inputs", "register_outputs", "value_types"):
                body[key] = record[key]
            if "unknown" in record:
                body["unknown"] = record["unknown"]
            for memory in body["memory"]:
                memory.update(record["memory"].get(str(memory["instruction"]), {}))
            if isinstance(record["calls"], list):
                body["calls"] = record["calls"]
            else:
                for call in body["calls"]:
                    for reg, value in call["arguments"].items():
                        value.update(record["calls"].get(str(call["instruction"]), {}).get(reg, {"defined": False}))
            if isinstance(record["returns"], list):
                body["returns"] = record["returns"]
            else:
                for exit_ in body["returns"]:
                    for reg, value in exit_["values"].items():
                        value.update(record["returns"].get(str(exit_["instruction"]), {}).get(reg, {"defined": False}))
        return item
