"""Load feature modules and their import-time evidence registrations."""

from importlib import import_module


def load() -> None:
    for name in (
        "unbake.decomp.checks",
        "unbake.decomp.symbols",
        "unbake.layout.structs",
        "unbake.layout.xver",
    ):
        import_module(name)
