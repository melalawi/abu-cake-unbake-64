"""Native report response fixtures for report parsing and publication tests."""

import json
import subprocess
from pathlib import Path

from unbake.objects.elf import Object


def generate(command, **kwargs):
    assert command[1:3] == ["report", "generate"], command
    root = Path(command[command.index("--project") + 1])
    units = json.loads((root / "objdiff.json").read_bytes())["units"]
    total = complete = matched = scored = completed = 0
    for unit in units:
        target = Object(unit["target_path"])
        text = target.section(".text")
        code = target.content(text)
        total += len(code)
        if unit.get("metadata", {}).get("complete"):
            complete += len(code)
            completed += 1
        if "base_path" in unit:
            base = Object(unit["base_path"])
            other = base.content(base.section(".text"))
            if other == code:
                matched += len(code)
                scored += len(code)
            else:
                # Native fixture score: a single low-bit instruction change retains 99.5%.
                scored += len(code) * (0.9975 if len(code) == 16 else 2 / 3)
    measures = dict(
        matched_code=matched,
        complete_code=complete,
        total_code=total,
        complete_units=completed,
        total_units=len(units),
        fuzzy_match_percent=scored / total * 100 if total else 0,
    )
    Path(command[command.index("--output") + 1]).write_text(json.dumps(dict(version=2, measures=measures, units=units)))
    return subprocess.CompletedProcess(command, 0, "", "")


def install(case):
    from tests.kit import boundary
    from unbake.report import progress

    mock = boundary(progress, generate)
    mock.start()
    case.addCleanup(mock.stop)
