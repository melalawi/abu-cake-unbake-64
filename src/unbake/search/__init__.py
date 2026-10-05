"""Composable source search methods."""

from importlib import import_module
from typing import cast

from unbake.config import Held
from unbake.search.core import Generator

METHODS: dict[str, Generator] = {}
BUILTINS = ("types", "registers", "order", "permute")


def available() -> tuple[str, ...]:
    """Expose the same names accepted by generator selection."""
    return tuple(dict.fromkeys((*BUILTINS, *METHODS)))


def register(name: str, generator: Generator) -> None:
    if not name or name == "list" or name in METHODS or not callable(getattr(generator, "propose", None)):
        raise Held("search", f"method {name}: unique name and Generator required")
    METHODS[name] = generator


def methods(names: str) -> list[Generator]:
    result: list[Generator] = []
    for name in names.split(","):
        if name not in METHODS:
            if name not in BUILTINS:
                raise Held("search", f"method {name}: unknown generator; available: {', '.join(available())}")
            if name == "permute":
                raise Held("search", "method permute: version and target_object are required")
            module = import_module(f"unbake.search.{name}")
            if not callable(getattr(module, "propose", None)):
                raise Held("search", f"method {name}.propose: missing value")
            result.append(cast(Generator, module))
        else:
            result.append(METHODS[name])
    return result
