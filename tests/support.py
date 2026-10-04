"""Temporary policies and names for mocked tool boundaries."""

import json
import os
from dataclasses import asdict
from pathlib import Path

from unbake import config


def tool(name: str) -> str:
    fields = {
        "mips-linux-gnu-as": "mips_as",
        "mips-linux-gnu-ld": "mips_ld",
        "mips-linux-gnu-objcopy": "mips_objcopy",
        "mips-linux-gnu-objdump": "mips_objdump",
        "mips-linux-gnu-readelf": "mips_readelf",
        "cpp": "cpp",
    }
    if name not in fields:
        raise config.Held("test", f"test policy tool {name}: unknown key")
    field = fields[name]
    executable = getattr(config.load_policy(), field)
    return str(executable)


def write_policy(root: Path) -> Path:
    original = config.load_policy()
    values = asdict(original)
    values["cache_root"] = root / "cache"
    values["state_root"] = root / "state"
    path = root / "policy.toml"
    path.write_text(
        "".join(
            f"{key} = {json.dumps(str(value) if isinstance(value, Path) else value)}\n" for key, value in values.items()
        )
    )
    return path


def test_policy(root: Path | None = None) -> config.Policy:
    explicit = os.environ.get("UNBAKE_TEST_POLICY") or os.environ.get("UNBAKE_POLICY")
    policy = (
        config.load_policy(write_policy(root))
        if root is not None
        else config.load_policy(Path(explicit) if explicit else None)
    )
    return policy
