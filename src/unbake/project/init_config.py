"""Render bootstrap project facts from measured cartridge assignments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from unbake.project import rom
from unbake.project.config import Held, Policy
from unbake.project.toolchain import CompilerSpec


def required(policy: Policy, name: str) -> Any:
    value = getattr(policy, name, None)
    if value is None:
        raise Held("init", f"policy.{name}: missing value")
    return value


def write_config(
    root: Path,
    name: str,
    title: str,
    cartridges: list[rom.Rom],
    names: dict[Path, str],
    names_from: str,
    assignments: dict[str, str],
    specs: dict[str, CompilerSpec],
    policy: Policy,
) -> None:
    ids = list(dict.fromkeys(assignments.values()))
    if not ids:
        raise Held("init", "compilers: no region assignments")
    quote = json.dumps
    text = [
        "[project]",
        f"name = {quote(name)}",
        f"title = {quote(title)}",
        f"names_from = {quote(names_from)}",
        f"versions = {quote(list(names.values()))}",
        f"default_compiler = {quote(ids[0])}",
        "",
        "[paths]",
        'src = "src"',
        'include = ["include"]',
        'asm = "asm"',
        'tools = "tools"',
    ]
    for cartridge in cartridges:
        version = names[cartridge.path]
        text.extend(
            [
                "",
                f"[version.{quote(version)}]",
                f"baserom_sha1 = {quote(cartridge.sha1)}",
                f"split = {quote(f'versions/{version}/{name}.yaml')}",
                f"symbols = {quote(f'versions/{version}/symbol_addrs.txt')}",
                "macros = []",
            ]
        )
    for ident in ids:
        text.extend(["", f"[compilers.{quote(ident)}]", f"cflags = {quote(list(specs[ident].cflags))}"])
    text.extend(["", "[units]"])
    text.extend(f"{quote(unit)} = {quote(ident)}" for unit, ident in assignments.items())
    text.extend(["", "[build]"])
    for key, policy_key in (
        ("ld", "mips_ld"),
        ("objcopy", "mips_objcopy"),
        ("splat", "splat"),
        ("as", "mips_as"),
        ("cpp", "cpp"),
    ):
        required(policy, policy_key)
        text.append(f"{key} = {quote('policy:' + policy_key)}")
    for key in ("asflags", "cppflags"):
        text.append(f"{key} = {quote(list(required(policy, key)))}")
    gcc = next((ident for ident in ids if specs[ident].family == "gcc"), None)
    if gcc:
        text.append(f"sn64_asflags = {quote(list(required(policy, 'sn64_asflags')))}")
    (root / "config.toml").write_text("\n".join(text) + "\n")
