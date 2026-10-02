"""Refine register definedness without replacing the mapped storage facts."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Project
from unbake.typemap import storage
from unbake.typemap.mips import Analysis


def refine(project: Project, facts: dict[str, Any]) -> dict[str, Any]:
    """Keep the original map shard; pin a separate ABI evidence supplement."""
    analyzer = storage.file_digest(Path(__file__).with_name("mips.py"))
    if facts.get("abi_analysis_sha256") == analyzer:
        return facts
    key = storage.digest((facts["shard_sha256"] + analyzer + storage.file_digest(Path(__file__))).encode())
    index = project.build / "map" / ("abi-index-" + key + ".json")
    if index.is_file():
        pointer = storage.read(index, "map.abi")
        filename = pointer.get("path")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise Held("solve", "map.abi.path: invalid ABI supplement name")
        path = index.parent / filename
        storage.verify_file(path, pointer["sha256"], "map.abi")
        supplement = storage.read(path, "map.abi")
        storage.validate_identity(project, supplement, "map.abi")
    else:
        functions = facts["functions"]
        inventory = {
            name: {
                "versions": {
                    version: {key: body[key] for key in ("address", "start", "end", "target_sha256")}
                    for version, body in item["versions"].items()
                }
            }
            for name, item in functions.items()
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
        records: dict[str, Any] = {}
        for name, item in inventory.items():
            records[name] = {}
            for version, body in item["versions"].items():
                with project.version(version).baserom.open("rb") as stream:
                    stream.seek(body["start"])
                    binary = stream.read(body["end"] - body["start"])
                if hashlib.sha256(binary).hexdigest() != body["target_sha256"]:
                    raise Held("solve", f"map.abi.target_sha256: {version}: {name}: ROM target changed")
                words = [word for (word,) in struct.iter_unpack(">I", binary)]
                result = Analysis(
                    name, version, body["address"], body["start"], words, targets[version], symbols[version]
                ).run()
                records[name][version] = {
                    "calls": {
                        str(call["instruction"]): {
                            reg: {key: value[key] for key in ("defined", "dependencies")}
                            for reg, value in call["arguments"].items()
                        }
                        for call in result["calls"]
                    },
                    "returns": {
                        str(exit_["instruction"]): {
                            reg: {key: value[key] for key in ("defined", "dependencies")}
                            for reg, value in exit_["values"].items()
                        }
                        for exit_ in result["returns"]
                    },
                }
        supplement = {**storage.identity(project), "map_shard_sha256": facts["shard_sha256"], "functions": records}
        content = storage.encoded(supplement)
        digest = storage.digest(content)
        path = index.parent / ("abi-facts-" + digest + ".json")
        storage.write(path, content)
        storage.write(index, storage.encoded({**storage.identity(project), "path": path.name, "sha256": digest}))
    return {
        **facts,
        "abi_supplement": {"path": path.name, "sha256": storage.file_digest(path)},
        "functions": Functions(facts["functions"], supplement["functions"]),
    }


class Functions(Mapping[str, dict[str, Any]]):
    """Overlay compact ABI observations during bounded map iteration."""

    def __init__(self, source: Mapping[str, dict[str, Any]], supplement: dict[str, Any]) -> None:
        self.source, self.supplement = source, supplement
        self.inventory = getattr(source, "inventory", source)

    def __len__(self) -> int:
        return len(self.source)

    def __iter__(self) -> Iterator[str]:
        return iter(self.source)

    def __getitem__(self, name: str) -> dict[str, Any]:
        item = self.source[name]
        for version, body in item["versions"].items():
            record = self.supplement[name][version]
            for call in body["calls"]:
                for reg, value in call["arguments"].items():
                    value.update(record["calls"].get(str(call["instruction"]), {}).get(reg, {"defined": False}))
            for exit_ in body["returns"]:
                for reg, value in exit_["values"].items():
                    value.update(record["returns"].get(str(exit_["instruction"]), {}).get(reg, {"defined": False}))
        return item
