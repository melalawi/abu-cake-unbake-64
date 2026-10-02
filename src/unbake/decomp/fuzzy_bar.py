"""The owner's all-containing-version admission rule for guarded C drafts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from unbake.decomp.trial_compare import TYPES


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]
    lines: tuple[str, ...]


def evaluate(compares: Mapping[str, Any], versions: Iterable[str], preconditions: Iterable[str] = ()) -> Verdict:
    """Require complete evidence, >=90% exact words, and only allowed differences.

    Integer arithmetic makes the 90% boundary independent of objdiff's score
    and floating-point rounding. Both live Compare and persisted rows are read.
    """
    expected = tuple(versions)
    reasons = list(preconditions)
    lines = []
    if not expected:
        reasons.append("owner.fuzzy_bar.versions: no containing versions")
    for version in sorted(set(expected) - set(compares)):
        reasons.append(f"owner.fuzzy_bar.{version}: containing version not compared")
    for version in sorted(set(compares) - set(expected)):
        reasons.append(f"owner.fuzzy_bar.{version}: not a containing version")
    for version in expected:
        if version not in compares:
            continue
        result = compares[version]

        def value(key: str, result: Any = result) -> Any:
            return result.get(key) if isinstance(result, Mapping) else getattr(result, key, None)

        exact, total, typed = value("identical"), value("of"), value("typed")
        if type(exact) is not int or type(total) is not int or not 0 <= exact <= total or total <= 0:
            reasons.append(f"owner.fuzzy_bar.{version}: missing/invalid exact-word counts")
            continue
        lines.append(f"owner fuzzy bar {version}: exact words {exact}/{total} ({100 * exact / total:.6f}%)")
        if 10 * exact < 9 * total:
            reasons.append(f"owner.fuzzy_bar.{version}: exact words {exact}/{total} below 90%")
        if not isinstance(typed, Mapping):
            reasons.append(f"owner.fuzzy_bar.{version}: typed differences missing")
            continue
        for kind in TYPES:
            count = typed.get(kind)
            if type(count) is not int or count < 0:
                reasons.append(f"owner.fuzzy_bar.{version}: typed.{kind} missing/invalid")
            elif kind not in ("register", "order", "relocation") and count:
                reasons.append(f"owner.fuzzy_bar.{version}: typed.{kind}={count}; required zero")
        for kind in sorted(set(typed) - set(TYPES)):
            reasons.append(f"owner.fuzzy_bar.{version}: unknown typed difference {kind}")
    lines.append("owner fuzzy bar: " + ("FAIL" if reasons else "PASS"))
    lines.extend("reason: " + reason for reason in reasons)
    return Verdict(not reasons, tuple(reasons), tuple(lines))
