"""Lower explicit private table dispatches with source-proved destination labels."""

import re

from unbake.project.config import Held

LABEL_ARRAY = re.compile(
    r"static\s+void\s*\*\s*\w+\[0\]\s*__attribute__\s*\(\(section\(\"\.sdata\"\)\)\)\s*=\s*"
    r"\{(?P<labels>[^}]+)\}\s*;"
)


def lower(source: str, name: str, entries: int) -> str:
    """Use a retained label list only when it proves every table destination."""
    dispatch = re.search(r"goto\s*\*\s*" + re.escape(name) + r"\s*\[(?P<index>[^\[\]]+)\]\s*;", source)
    if dispatch is None:
        raise Held("rodata", f"{name}: unsupported explicit table indexing")
    arrays = list(LABEL_ARRAY.finditer(source))
    if len(arrays) != 1:
        raise Held("rodata", f"{name}: required one complete retained destination-label list")
    array = arrays[0]
    labels = re.findall(r"&&\s*([A-Za-z_]\w*)", array["labels"])
    if len(labels) != entries or any(not re.search(r"\b" + label + r"\s*:", source) for label in labels):
        raise Held("rodata", f"{name}: destination-label list does not cover the complete table")
    replacement = "switch (" + dispatch["index"] + ") {\n"
    replacement += "".join(f"case {index}: goto {label};\n" for index, label in enumerate(labels))
    replacement += "}\n"
    source = source[: dispatch.start()] + replacement + source[dispatch.end() :]
    # The retained list is compiler-specific address-label syntax; the switch
    # itself now keeps all labels reachable in ordinary C.
    source = LABEL_ARRAY.sub("", source, count=1)
    return source
