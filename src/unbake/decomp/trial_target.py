"""Select real target objects using the build's own inventory."""

import json
import re
from pathlib import Path

from unbake.project.config import Held
from unbake.project_tools.elf import Object


def target_object(generation: Path, function: str, version: str) -> Path:
    config = generation / "objdiff.json"
    if config.is_file():
        document = json.loads(config.read_bytes())
        for unit in document.get("units", []):
            if unit.get("name") == function and not unit.get("metadata", {}).get("complete"):
                path = (generation / str(unit["target_path"])).resolve()
                if path.is_file():
                    return path
    # Matched units in report configs may contain linked-byte wrappers. Select
    # their original relocatable input from the generated linker inventory.
    inventory: set[str] = set()
    for script in generation.glob("*.ld"):
        inventory.update(re.findall(r"obj/(?:src|asm)/[^\s()\";]+\.o", script.read_text()))
    for name in sorted(inventory):
        path = generation / name
        if not path.is_file():
            continue
        obj = Object(path)
        if any(
            s["name"] == function and s["info"] & 15 == 2 and s["section"]
            for table in obj.symbols.values()
            for s in table
        ):
            return path.resolve()
    raise Held("try", f"VERSION {version}: target object for {function} is missing from build inventory {generation}")
