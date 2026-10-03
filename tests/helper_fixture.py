"""Call project helper entry points directly with mocked external-tool output."""

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


def call(module, arguments, root):
    out, error = io.StringIO(), io.StringIO()
    previous = Path.cwd()
    status = 0
    try:
        os.chdir(root)
        with (
            patch.object(sys, "argv", [module.__file__, *map(str, arguments)]),
            redirect_stdout(out),
            redirect_stderr(error),
        ):
            try:
                module.main()
            except SystemExit as result:
                status = result.code or 0
    finally:
        os.chdir(previous)
    return subprocess.CompletedProcess(arguments, status, out.getvalue(), error.getvalue())


def extraction(project, version="us", generation=None):
    from unbake.project_tools import extract

    if generation is None:
        extract.prepare_build(project.build_link(version))
    configured = project.version(version)
    generation = generation or project.build_link(version)
    return call(
        extract,
        [
            "--split",
            configured.split,
            "--symbols",
            configured.symbols,
            "--baserom",
            configured.baserom,
            "--build",
            generation,
            "--asm",
            project.asm / version,
            "--src",
            project.src,
            "--non-matching",
            "0",
            "--name",
            project.name,
            "--splat",
            project.tools / "splat",
            "--recipe",
            project.tools / "build.json",
        ],
        project.root,
    )
