"""Give m2c disjoint register pairs for a compiler's independent 64-bit FPRs."""

import re

from unbake.config import Held
from unbake.process import named as cause_named


def command(executable: str, assembly: str) -> list[str]:
    """Extend the configured Python decompiler's analysis register namespace."""
    import shlex
    from pathlib import Path

    if not re.search(r"\$f(?:3[2-9]|[4-9][0-9])\b", assembly):
        return [executable]
    path = Path(executable)
    text = path.read_text()
    if "m2c.main" not in text:
        raise Held(
            cause_named(
                "decomp.draft_fp.command",
                "configured decompiler cannot represent virtual independent FPR pairs",
                owner="decomp.draft_fp",
                stage="m2c",
            )
        )
    launcher = shlex.split(text.splitlines()[0].removeprefix("#!"))
    if launcher and Path(launcher[0]).name in ("sh", "bash"):
        wrapped = re.search(r"(?m)^'''exec'\s+(.+?)\s+\"\$0\"", text)
        launcher = shlex.split(wrapped[1]) if wrapped else []
    if not launcher or not any("python" in Path(item).name for item in launcher):
        raise Held(
            cause_named(
                "decomp.draft_fp.command",
                "configured decompiler has no Python interpreter for virtual FPR analysis",
                owner="decomp.draft_fp",
                stage="m2c",
            )
        )
    return [*launcher, str(Path(__file__).parents[1] / "decomp/m2c_registers.py"), executable]
