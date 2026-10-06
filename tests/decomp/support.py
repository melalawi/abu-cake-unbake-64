"""Draft-side fixture: the shared project fixture with preprocessing and the layout map patched in process."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import boundary, script_output
from tests.project_fixture import make
from unbake.config import Host, Project

SCRATCH_ROOT = Path(tempfile.gettempdir()).resolve()


def fixture(
    directory: Path, words: list[int] | None = None, versions: tuple[str, ...] = ("us",), *, case
) -> tuple[Project, Host, Path]:
    """A ready project, its host and a NON_MATCHING alpha draft; cpp runs in process (tests.preprocessor)."""
    from tests.preprocessor import output
    from unbake import process
    from unbake.layout import map as ownership

    project, host = make(directory, words, versions)

    def frontend(command, **kwargs):
        """Tool processes in process: cpp and cc -E through the fixture preprocessor, the fixture compiler
        writes its -o file, test-authored scripts run in place. argv[0] resolves against cwd, as exec does."""
        import subprocess

        executable = Path(kwargs.get("cwd", ".")) / command[0]
        if "cpp" in executable.name or "-E" in command:
            return output(command, **kwargs)
        if executable.name == "cc" and executable.is_relative_to(project.tools):
            if getattr(case, "compiler_error", ""):
                return subprocess.CompletedProcess(command, 1, "", case.compiler_error)
            if "-o" in command:
                (Path(kwargs.get("cwd", ".")) / command[command.index("-o") + 1]).write_bytes(b"compiler output")
            return subprocess.CompletedProcess(command, 0, "", "")
        return script_output([str(executable), *command[1:]], **kwargs)

    for module, execute in ((process, frontend),):
        mock = boundary(module, execute)
        mock.start()
        case.addCleanup(mock.stop)
    source = directory / "alpha.c"
    source.write_text("/* NON_MATCHING: returns one. */\nint alpha(void) { return 1; }\n", encoding="utf-8")

    def synthetic_map(selected: SimpleNamespace) -> ownership.Map:
        members = ownership.catalog(selected)
        extra = {p.stem for p in selected.src.rglob("*.c")} | {"f", "g", "api"}
        for offset, name in enumerate(sorted(extra - members.keys())):
            members[name] = ownership.Member(name, "main", 0x90000000 + offset * 4, selected.versions)
        return ownership.Map(
            2, tuple(ownership.Group(m.name, m.segment, "default", (m.name,)) for m in members.values())
        )

    mapping = patch.object(ownership, "load", side_effect=synthetic_map)
    mapping.start()
    case.addCleanup(mapping.stop)
    return project, host, source


@contextmanager
def solved(database: dict) -> Iterator[Path]:
    """A stand-in types.sqlite path whose rows are DATABASE's (kind -> name -> row), read by name as types_db does."""

    def entries(_file, kind, names):
        rows = database.get(kind, {})
        return {name: rows[name] for name in set(names) if name in rows}

    with patch("unbake.typemap.types_db.entries", entries):
        yield Path("types.sqlite")
