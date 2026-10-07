"""One ordering for measured draft candidates, with exact evidence first."""

from collections.abc import Mapping

from unbake.work.score import Measurement, weakest


def candidate_rank(
    identical_everywhere: bool,
    identical_words: int,
    typed_differences: int,
    scores: Mapping[str, float],
) -> tuple[bool, int, int, float]:
    """Ascending key: complete identity, exact words, differences, then objdiff."""
    return not identical_everywhere, -identical_words, typed_differences, -weakest(dict(scores))


def measured_candidate_rank(
    compares: Mapping[str, Measurement], fuzzy: float | None = None
) -> tuple[bool, int, int, float]:
    """Adapt live trial evidence to the same ordering as retained draft records."""
    return candidate_rank(
        bool(compares) and all(item.exact for item in compares.values()),
        sum(item.identical_words or 0 for item in compares.values()),
        sum(sum((item.typed or {}).values()) for item in compares.values()),
        {version: item.percent for version, item in compares.items() if item.percent is not None}
        if fuzzy is None
        else {"weakest": fuzzy},
    )
