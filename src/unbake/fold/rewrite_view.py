"""Expand source using compiler-owned spelling-location evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from unbake.compilers.families.types import View
from unbake.config import Host, Project

_BOUNDARY = "__unbake_rewrite_boundary"


def prepare(
    project: Project,
    policy: Host,
    source: str,
    version: str,
    source_path: Path,
    *,
    preprocess: Callable[[Project, list[str], str], str] | None = None,
    contents: dict[Path, str] | None = None,
) -> View:
    """Expand the fold's effective headers, compiler defines and source macros."""
    from unbake.compilers import drivers
    from unbake.compilers.families import family_for
    from unbake.decomp.draft_context import ordered_headers
    from unbake.project.headers import include_headers
    from unbake.typemap import declarations, storage

    if contents is None:
        contents = {
            path: path.read_text()
            for path, _ in include_headers(project, exclude=storage.generated_view(project))
            if not storage.generated(project, path)
        }
    prelude = "".join(f"#include {json.dumps(str(path))}\n" for path in ordered_headers(contents))
    prelude += "".join(f"#include {json.dumps(str(path))}\n" for path in declarations._generated_context(project))
    filename = str(source_path)
    unit = prelude + f"extern int {_BOUNDARY};\n#line 1 {json.dumps(filename)}\n" + source
    command = drivers.analysis_command(project, policy, version, source_path.stem)
    command[-1:-1] = [
        "-DUNBAKE_PROTOTYPES_H",
        *family_for(project.compiler_for(source_path.stem)).analysis_location_flags(),
        "-iquote",
        str(source_path.parent),
    ]
    output = (preprocess or declarations._preprocess)(project, command, unit)
    return family_for(project.compiler_for(source_path.stem)).token_view(
        output, source, filename, prelude.count("\n") + 1
    )
