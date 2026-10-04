"""Draft-side fixture: the shared project fixture with preprocessing and the layout map patched in process."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import boundary
from tests.project_fixture import make
from unbake.config import Host, Project

SCRATCH_ROOT = Path(tempfile.gettempdir()).resolve()


def fixture(
    directory: Path, words: list[int] | None = None, versions: tuple[str, ...] = ("us",), *, case
) -> tuple[Project, Host, Path]:
    """A ready project, its host and a NON_MATCHING alpha draft; cpp runs in process (tests.preprocessor)."""
    from tests.preprocessor import output
    from unbake.layout import map as ownership
    from unbake.layout import structs
    from unbake.typemap import declarations

    for module in (structs, declarations):
        mock = boundary(module, output)
        mock.start()
        case.addCleanup(mock.stop)
    project, host = make(directory, words, versions)
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
