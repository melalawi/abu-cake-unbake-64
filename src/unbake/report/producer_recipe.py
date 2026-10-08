"""Pure semantic projection of the generated producer's USED Make expressions.

This is not a Make interpreter. Unknown functions, duplicate definitions and
cycles refuse projection. No commands are run and no variable-name aliases exist.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def definitions(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in text.replace("\\\n", " ").splitlines():
        match = re.fullmatch(r"([A-Za-z_][\w.]*)\s*(?::|\?)?=\s*(.*)", line)
        if match:
            if match[1] in rows:
                raise ValueError(f"producer.recipe: duplicate definition {match[1]}")
            rows[match[1]] = match[2]
    return rows


def _split(value: str, separator: str = ",") -> list[str]:
    depth, start, result = 0, 0, []
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == separator and depth == 0:
            result.append(value[start:index])
            start = index + 1
    result.append(value[start:])
    return result


def expand(value: str, rows: Mapping[str, str], leaves: Mapping[str, str], seen: tuple[str, ...] = ()) -> str:
    result, index = [], 0
    while index < len(value):
        if not value.startswith("$(", index) or (index and value[index - 1] == "$"):
            result.append(value[index])
            index += 1
            continue
        end, depth = index + 2, 1
        while end < len(value) and depth:
            depth += (value[end] == "(") - (value[end] == ")")
            end += 1
        if depth:
            raise ValueError("producer.recipe: unbalanced Make expression")
        expression = value[index + 2 : end - 1]
        name, _, arguments = expression.partition(" ")
        if name == "if":
            args = _split(arguments)
            if len(args) not in (2, 3):
                raise ValueError("producer.recipe: malformed if")
            condition = expand(args[0], rows, leaves, seen).strip()
            selected = args[1] if condition else args[2] if len(args) == 3 else ""
            replacement = expand(selected, rows, leaves, seen)
        elif name in ("firstword", "word", "subst", "addprefix", "basename", "notdir"):
            args = [expand(part, rows, leaves, seen) for part in _split(arguments)]
            if name == "notdir" and len(args) == 1:
                replacement = " ".join(word.rsplit("/", 1)[-1] for word in args[0].split())
            elif name == "basename" and len(args) == 1:
                replacement = " ".join(word.rsplit(".", 1)[0] for word in args[0].split())
            elif name == "firstword" and len(args) == 1:
                replacement = next(iter(args[0].split()), "")
            elif name == "word" and len(args) == 2 and args[0].isdigit():
                words = args[1].split()
                replacement = words[int(args[0]) - 1] if 0 < int(args[0]) <= len(words) else ""
            elif name == "subst" and len(args) == 3:
                replacement = args[2].replace(args[0], args[1])
            elif name == "addprefix" and len(args) == 2:
                replacement = " ".join(args[0] + word for word in args[1].split())
            else:
                raise ValueError(f"producer.recipe: malformed {name}")
        elif name == "abspath":
            replacement = leaves.get(expression, expand(arguments, rows, leaves, seen))
        elif name == "shell":
            # A command remains an expression, not executed or treated as its output.
            replacement = "$(shell " + expand(arguments, rows, leaves, seen) + ")"
        elif " " in expression:
            raise ValueError(f"producer.recipe: unsupported function {name}")
        else:
            name = expand(expression, rows, leaves, seen) if "$(" in expression else expression
            if name in leaves:
                replacement = leaves[name]
            elif name in seen:
                raise ValueError("producer.recipe: cyclic expression")
            elif name in rows:
                replacement = expand(rows[name], rows, leaves, (*seen, name))
            else:
                replacement = "$(" + name + ")"
        result.append(replacement)
        index = end
    return "".join(result)


def body(text: str, root: str, leaves: Mapping[str, str]) -> str:
    rows = definitions(text)
    if root not in rows:
        raise ValueError(f"producer.recipe: missing producer {root}")
    return expand(rows[root], rows, leaves)


def toolchain_files(text: str) -> tuple[str, ...]:
    rows = definitions(text)
    value = rows.get("TOOLCHAIN", "")
    match = re.fullmatch(r"\$\(firstword \$\(shell cat ([\w./ -]+) \| sha1sum\)\)", value)
    if match is None:
        raise ValueError("producer.recipe: unproved native key toolchain algorithm")
    return tuple(match[1].split())


def unit_definitions(text: str, unit: str, version: str) -> dict[str, str]:
    """Read generated target-specific assignments for this one producer."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(.+):\s*([A-Za-z_][\w.]*)\s*(?::|\?)?=\s*(.*)", line)
        if match is None:
            continue
        targets = match[1].split()
        selected = [p for p in targets if f"/{unit}." in p and (f"/{version}/" in p or "/%/" in p)]
        if selected:
            result[match[2]] = match[3]
    return result


def used_identity(certificate: Mapping[str, Any]) -> dict[str, object]:
    """Captured digests bind origin; equality concerns the used expressions."""
    return {path: row["used"] for path, row in certificate.items()}


def key_salts(text: str, used: Mapping[str, str]) -> tuple[str, ...]:
    """Key-envelope metadata is distinct from files consumed by native commands.

    Only generated tool metadata can have this role. Source, providers, scripts,
    binaries and helpers keep their ordinary content pins even if also salted.
    """
    from pathlib import PurePosixPath

    commands = "\n".join(value for root, value in used.items() if root != "UNIT_KEY")
    result = []
    for name in toolchain_files(text):
        path = PurePosixPath(name)
        if (
            path.parts[0] == "tools"
            and ".." not in path.parts
            and path.suffix in {".sha256", ".version"}
            and name not in commands
        ):
            result.append(name)
    return tuple(result)


def argv_identity(words: list[str]) -> tuple[tuple[str, ...], ...]:
    """Exclusive groups are resolved independently for each native command."""
    from unbake.compilers.recipe_options import merge_options

    commands: list[tuple[str, ...]] = []
    start = 0
    for i, token in enumerate([*words, "&&"]):
        if token == "&&":
            commands.append(merge_options(tuple(words[start:i])))
            start = i + 1
    return tuple(commands)
