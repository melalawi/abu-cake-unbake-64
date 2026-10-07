"""Resolve interior global references through declared aggregate layouts."""

import re
from collections.abc import Sequence
from pathlib import Path

from unbake.config import Held, Host, Project
from unbake.decomp.symbols import Reference
from unbake.layout.structs import layouts, preprocess
from unbake.process import named as cause_named


def resolve(
    project: Project, policy: Host, source: Path, version: str, values: dict[str, int], refs: Sequence[Reference]
) -> tuple[set[int], list[str]]:
    """Return settled addresses and field guidance for typed global objects."""
    if not refs or not source.is_file():
        return set(), []
    text = preprocess(source, project, policy, version)
    records = {name: record for record in layouts(text) for name in (record.name, *record.aliases)}
    qualifier = r"(?:(?:const|volatile|restrict|__restrict)\s+)*"
    declarations = re.findall(rf"\bextern\s+{qualifier}(?:(?:struct|union)\s+)?(\w+)\s+{qualifier}(\w+)\s*;", text)
    objects = [
        (name, values[name], records[type_]) for type_, name in declarations if type_ in records and name in values
    ]
    settled: set[int] = set()
    output = []
    for ref in refs:
        candidates = [
            (name, base, record)
            for name, base, record in objects
            if base <= ref.address and ref.address + ref.size <= base + record.size
        ]
        if candidates:
            nearest = max(base for _, base, _ in candidates)
            candidates = list(
                {
                    (name, base, record.name): (name, base, record)
                    for name, base, record in candidates
                    if base == nearest
                }.values()
            )
        if len(candidates) > 1:
            raise Held(
                cause_named(
                    f"0x{ref.address:X}",
                    f"0x{ref.address:X}: ambiguous aggregate symbol layout",
                    owner="decomp.guide_layout",
                    stage="guide",
                )
            )
        if not candidates:
            continue
        name, base, record = candidates[0]
        offset = ref.address - base
        fields = [
            field
            for field in record.fields
            if field.offset <= offset and offset + ref.size <= field.offset + field.size
        ]
        field_name = f" ({name}.{fields[0].name})" if len(fields) == 1 else ""
        output.append(f"reference: 0x{ref.address:08X} = {name}+0x{offset:X}{field_name}; layout {record.name}")
        settled.add(ref.address)
    return settled, list(dict.fromkeys(output))
