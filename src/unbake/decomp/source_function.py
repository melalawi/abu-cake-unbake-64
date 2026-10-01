"""Select a draft function independently of its source filename."""

import re
from pathlib import Path

from unbake.project.config import Held


def definitions(text: str) -> tuple[str, ...]:
    """Read top-level function bodies without depending on header typedefs."""
    clean = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: "".join("\n" if char == "\n" else " " for char in match[0]),
        text,
        flags=re.S,
    )
    clean = re.sub(r"^[ \t]*#(?:\\\n|[^\n])*", "", clean, flags=re.M)
    tokens = re.findall(r"[A-Za-z_]\w*|[^\s]", clean)
    stack: list[int] = []
    pairs: dict[int, int] = {}
    depth = 0
    names = []
    for index, token in enumerate(tokens):
        if token == "(":
            stack.append(index)
        elif token == ")" and stack:
            pairs[index] = stack.pop()
        elif token == "{":
            if depth == 0 and index and tokens[index - 1] == ")":
                opening = pairs.get(index - 1, 0)
                name = tokens[opening - 1] if opening else ""
                if re.fullmatch(r"[A-Za-z_]\w*", name) and name not in ("if", "for", "while", "switch"):
                    names.append(name)
            depth += 1
        elif token == "}":
            depth -= 1
    return tuple(dict.fromkeys(names))


def select(source: Path, text: str, function: str | None = None, *, phase: str = "try") -> str:
    """Use an explicit identifier or the source's single defined function."""
    names = definitions(text)
    if function is not None:
        if not re.fullmatch(r"[A-Za-z_]\w*", function):
            raise Held(phase, f"function {function!r}: expected C identifier")
        return function
    if len(names) != 1:
        raise Held(
            phase,
            f"source {source}: function ambiguous; definitions: {', '.join(names) or '<none>'}; select --function NAME",
        )
    return names[0]
