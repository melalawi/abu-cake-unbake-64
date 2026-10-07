"""Composable source search methods."""

from importlib import import_module
from typing import cast

from unbake.config import Held
from unbake.process import named as cause_named
from unbake.search.core import Generator

METHODS: dict[str, Generator] = {}
BUILTINS = ("types", "registers", "order", "permute", "scheduler-birth")


def available() -> tuple[str, ...]:
    """Expose the same names accepted by generator selection."""
    return tuple(dict.fromkeys((*BUILTINS, *METHODS)))


def register(name: str, generator: Generator) -> None:
    if not name or name == "list" or name in METHODS or not callable(getattr(generator, "propose", None)):
        raise Held(
            cause_named(
                "search.__init__.register",
                f"method {name}: unique name and Generator required",
                owner="search.__init__",
                stage="search",
            )
        )
    METHODS[name] = generator


def methods(names: str) -> list[Generator]:
    result: list[Generator] = []
    for name in names.split(","):
        if name not in METHODS:
            if name not in BUILTINS:
                raise Held(
                    cause_named(
                        "search.__init__.methods",
                        f"method {name}: unknown generator; available: {', '.join(available())}",
                        owner="search.__init__",
                        stage="search",
                    )
                )
            if name == "permute":
                raise Held(
                    cause_named(
                        "search.__init__.methods",
                        "method permute: version and target_object are required",
                        owner="search.__init__",
                        stage="search",
                    )
                )
            module = import_module(f"unbake.search.{name.replace('-', '_')}")
            if not callable(getattr(module, "propose", None)):
                raise Held(
                    cause_named(
                        "search.__init__.methods",
                        f"method {name}.propose: missing value",
                        owner="search.__init__",
                        stage="search",
                    )
                )
            result.append(cast(Generator, module))
        else:
            result.append(METHODS[name])
    return result
