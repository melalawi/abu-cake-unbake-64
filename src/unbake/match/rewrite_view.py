"""Compiler-expanded C tokens with their original spelling locations.

GCC's -fdebug-cpp emits a location map before every preprocessing token. Keep
those locations separately from the parse view: expanding a macro must never
turn a rewritten function into its preprocessed implementation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unbake.layout.structs import held
from unbake.project.config import Policy, Project

# Consume quoted tokens before looking for another map, including map-like text
# in literals. Multi-character punctuators must remain one preprocessing token.
_TOKEN = (
    r"(?:u8|[LuU])?\"(?:\\.|[^\"\\])*\"|(?:[LuU])?'(?:\\.|[^'\\])*'"
    r"|(?:\d|\.\d)(?:[\w.]|(?<=[eEpP])[+-])*|[A-Za-z_]\w*"
    r"|>>=|<<=|\.\.\.|->|\+\+|--|&&|\|\||<<|>>|<=|>=|==|!=|[+*/%&|^!-]=|\S"
)
_MAP = r"\{P:(.*?);F:.*?;L:(\d+);C:(\d+);S:\d+;M:[^;]+;E:[^}]+\}"
_OUTPUT = re.compile(f"(?:{_MAP})|({_TOKEN})", re.S)
_BOUNDARY = "__unbake_rewrite_boundary"


@dataclass(frozen=True)
class Location:
    file: str
    line: int
    column: int


@dataclass(frozen=True)
class View:
    text: str
    # One expanded token per line; no arithmetic involving header line counts.
    locations: tuple[Location, ...]
    origins: tuple[int | None, ...]


def decode(output: str, source: str, filename: str) -> View:
    """Retain source tokens after the header boundary and validate edit origins."""
    starts = [0, *(match.end() for match in re.finditer("\n", source))]
    pieces: list[str] = []
    locations: list[Location] = []
    origins: list[int | None] = []
    location: Location | None = None
    found = active = False
    for match in _OUTPUT.finditer(output):
        if match[1] is not None:
            location = Location(match[1], int(match[2]), int(match[3]))
            continue
        token = match[4]
        if not active:
            if found and token == ";":
                active = True
            elif token == _BOUNDARY:
                found = True
            continue
        if location is None:
            held("source types", f"{filename}:1: preprocessor token has no spelling location")
        origin = None
        if location.file == filename and 1 <= location.line <= len(starts):
            at = starts[location.line - 1] + location.column - 1
            # Pasted tokens and builtins can have an invocation location instead
            # of an editable spelling. Never guess where such tokens came from.
            if source[at : at + len(token)] == token:
                origin = at
        pieces.append(token + "\n")
        locations.append(location)
        origins.append(origin)
    if not active:
        held("source types", f"{filename}:1: preprocessor did not provide token locations (-fdebug-cpp)")
    return View("".join(pieces), tuple(locations), tuple(origins))


def prepare(
    project: Project,
    policy: Policy,
    source: str,
    version: str,
    source_path: Path,
    *,
    preprocess: Callable[[Project, list[str], str], str] | None = None,
) -> View:
    """Expand the fold's effective headers, compiler defines and source macros."""
    from unbake.decomp.draft_context import ordered_headers
    from unbake.project.headers import include_headers
    from unbake.typemap import declarations, storage
    from unbake.typemap.split import consumer_macro

    contents = {path: path.read_text() for path, _ in include_headers(project) if not storage.generated(project, path)}
    prelude = "".join(f"#include {json.dumps(str(path))}\n" for path in ordered_headers(contents))
    prelude += "".join(f"#include {json.dumps(str(path))}\n" for path in declarations._generated_context(project))
    if (project.include[0] / "shared/consumers" / (source_path.stem + ".h")).is_file():
        prelude += f"#define {consumer_macro(source_path.stem)} 1\n"
    filename = str(source_path)
    unit = prelude + f"extern int {_BOUNDARY};\n#line 1 {json.dumps(filename)}\n" + source
    command = declarations._cpp_command(project, policy, version, extra=True, line_markers=False)
    command[-1:-1] = [
        "-P",
        "-fdebug-cpp",
        "-ftrack-macro-expansion=2",
        "-ftabstop=1",
        "-iquote",
        str(source_path.parent),
    ]
    output = (preprocess or declarations._preprocess)(project, command, unit)
    return decode(output, source, filename)
