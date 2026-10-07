"""Typed trial evidence and ordered resolution into reviewed edits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, TypeAlias

from unbake.config import Held, Host, Project
from unbake.layout.split import Edit
from unbake.process import named as cause_named


@dataclass(frozen=True)
class SymbolNeed:
    version: str
    name: str
    address: int
    addend: int
    section: str
    type: str
    size: int
    evidence: object


@dataclass(frozen=True)
class LabelNeed:
    version: str
    name: str
    address: int
    row: object
    evidence: object


@dataclass(frozen=True)
class LayoutNeed:
    version: str
    struct: str
    fields: object
    source: str
    evidence: object


@dataclass(frozen=True)
class PlacementNeed:
    version: str
    function: str
    start: int
    end: int
    action: str
    value: object
    evidence: object


@dataclass(frozen=True)
class GuardFinding:
    rule: str
    line: int
    text: str
    fakematch: str | None = None


Need: TypeAlias = SymbolNeed | LabelNeed | LayoutNeed | PlacementNeed | GuardFinding
Resolver: TypeAlias = Callable[[list[Need], Project, Host], list[Edit]]
_KINDS = {kind.__name__: kind for kind in (SymbolNeed, LabelNeed, LayoutNeed, PlacementNeed, GuardFinding)}
RESOLVERS: dict[type[Need], tuple[int, Resolver]] = {}


def name(need: object) -> str:
    return str(
        next(
            (
                getattr(need, field)
                for field in ("name", "struct", "function", "rule", "section")
                if hasattr(need, field)
            ),
            type(need).__name__,
        )
    )


def register_resolver(kind: type[Need], order: int, resolver: Resolver) -> None:
    if kind not in _KINDS.values():
        raise Held(
            cause_named(
                "decomp.needs.register_resolver",
                f"resolver.kind {kind}: unknown need",
                owner="decomp.needs",
                stage="needs",
            )
        )
    if kind in RESOLVERS:
        raise Held(
            cause_named(
                "decomp.needs.register_resolver",
                f"resolver {kind.__name__}: already registered",
                owner="decomp.needs",
                stage="needs",
            )
        )
    if not callable(resolver) or isinstance(order, bool) or not isinstance(order, int):
        raise Held(
            cause_named(
                "decomp.needs.register_resolver",
                f"resolver {kind.__name__}: order and callable required",
                owner="decomp.needs",
                stage="needs",
            )
        )
    RESOLVERS[kind] = (order, resolver)


def resolvers() -> list[tuple[type[Need], int, Resolver]]:
    return [
        (kind, order, resolver)
        for kind, (order, resolver) in sorted(RESOLVERS.items(), key=lambda item: (item[1][0], item[0].__name__))
    ]


def encode(need: Need) -> dict[str, object]:
    if type(need) not in _KINDS.values():
        raise Held(
            cause_named("decomp.needs.encode", f"need {name(need)}: unknown kind", owner="decomp.needs", stage="needs")
        )
    return {"need_type": type(need).__name__, **{field.name: getattr(need, field.name) for field in fields(need)}}


def decode(row: Any) -> Need:
    kind = _KINDS.get(row.get("need_type", "")) if isinstance(row, dict) else None
    if kind is None:
        raise Held(cause_named("decomp.needs.decode", f"need {row}: unknown kind", owner="decomp.needs", stage="needs"))
    for field in fields(kind):
        if field.name not in row:
            raise Held(
                cause_named(
                    f"{kind.__name__}.{field.name}",
                    f"{kind.__name__}.{field.name}: missing value",
                    owner="decomp.needs",
                    stage="needs",
                )
            )
    return kind(**{field.name: row[field.name] for field in fields(kind)})


def resolve(
    pending: list[Need], project: Project, policy: Host, apply: Callable[[Project, Host, list[Edit]], object]
) -> list[str]:
    """Refuse unregistered kinds before writing, then apply each ordered batch."""
    for need in pending:
        if type(need) not in RESOLVERS:
            raise Held(
                cause_named(
                    "decomp.needs.resolve",
                    f"unresolved need {name(need)} ({type(need).__name__})",
                    owner="decomp.needs",
                    stage="match",
                )
            )
    resolved: list[str] = []
    for kind, _, resolver in resolvers():
        batch = [need for need in pending if type(need) is kind]
        if batch:
            edits = resolver(batch, project, policy)
            if edits is None:
                raise Held(
                    cause_named(
                        "decomp.needs.resolve",
                        f"unresolved need {name(batch[0])}: resolver returned no edits",
                        owner="decomp.needs",
                        stage="match",
                    )
                )
            apply(project, policy, edits)
            resolved.extend(name(need) for need in batch)
    return resolved
