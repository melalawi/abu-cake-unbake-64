"""Give m2c disjoint register pairs for a compiler's independent 64-bit FPRs."""

import re

from unbake.config import Held

_ALIASES = {
    name: index
    for index, name in enumerate(
        (
            "fv0",
            "fv0f",
            "fv1",
            "fv1f",
            "ft0",
            "ft0f",
            "ft1",
            "ft1f",
            "ft2",
            "ft2f",
            "ft3",
            "ft3f",
            "fa0",
            "fa0f",
            "fa1",
            "fa1f",
            "ft4",
            "ft4f",
            "ft5",
            "ft5f",
            "fs0",
            "fs0f",
            "fs1",
            "fs1f",
            "fs2",
            "fs2f",
            "fs3",
            "fs3f",
            "fs4",
            "fs4f",
            "fs5",
            "fs5f",
        )
    )
}


def register_pairs(assembly: str, flags: tuple[str, ...], function: str) -> str:
    """Rename analysis registers only, retaining o32 arguments/return and saves.

    m2c assumes FR=0 (two adjacent 32-bit FPRs per double). SN64 -mfp64
    uses FR=1, so loading f1 must not overwrite f0's supposed second half.
    An injective map into even registers represents each independent FPR as
    one disjoint m2c pair. Virtual pairs retain the original ABI save class
    when the physical register namespace is exhausted.
    """
    if "-mfp64" not in flags:
        return assembly
    token = re.compile(r"/\*.*?\*/|//[^\n]*|\$([A-Za-z0-9_]+)", re.S)

    def number(match: re.Match[str]) -> int | None:
        name = match[1]
        if name is None:
            return None
        if name in _ALIASES:
            return _ALIASES[name]
        return int(name[1:]) if re.fullmatch(r"f(?:[0-9]|[12][0-9]|3[01])", name) else None

    used = {value for match in token.finditer(assembly) if (value := number(match)) is not None}
    if not any(value % 2 for value in used):
        return assembly
    mapping = {value: value for value in used if value % 2 == 0}
    # f0, f12 and f14 are ABI locations even when absent from this body.
    reserved = {0, 12, 14} | set(mapping.values())
    for value in sorted(used - mapping.keys()):
        candidates = [
            item for item in range(0 if value < 20 else 20, 20 if value < 20 else 32, 2) if item not in reserved
        ]
        if not candidates:
            candidates = [32 + value * 2]
        mapping[value] = candidates[0]
        reserved.add(candidates[0])

    def rename(match: re.Match[str]) -> str:
        value = number(match)
        return f"$f{mapping[value]}" if value is not None else match[0]

    return token.sub(rename, assembly)


def command(executable: str, assembly: str) -> list[str]:
    """Extend the configured Python decompiler's analysis register namespace."""
    import shlex
    from pathlib import Path

    if not re.search(r"\$f(?:3[2-9]|[4-9][0-9])\b", assembly):
        return [executable]
    path = Path(executable)
    text = path.read_text()
    if "m2c.main" not in text:
        raise Held("m2c", "configured decompiler cannot represent virtual independent FPR pairs")
    launcher = shlex.split(text.splitlines()[0].removeprefix("#!"))
    if launcher and Path(launcher[0]).name in ("sh", "bash"):
        wrapped = re.search(r"(?m)^'''exec'\s+(.+?)\s+\"\$0\"", text)
        launcher = shlex.split(wrapped[1]) if wrapped else []
    if not launcher or not any("python" in Path(item).name for item in launcher):
        raise Held("m2c", "configured decompiler has no Python interpreter for virtual FPR analysis")
    return [*launcher, str(Path(__file__).parents[1] / "project_tools/m2c_registers.py"), executable]
