"""Resume a unit pass from the longest checkpointed prefix it shares with a recent unit.

Units that include the same headers share a long preprocessed prefix. A checkpoint is a line start
right after `;\\n` at bracket depth 0, before any quote, comment, line splice or directive. Every
pass resumed here restarts its state at a checkpoint, so its result over `prefix + rest` is its
state after `prefix` continued over `rest`. Each pass kind keeps the state of a few prefixes, so a
unit pays only for the text after the header expansion it shares with its neighbours.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar

from unbake import cache

S = TypeVar("S")
R = TypeVar("R")

PREFIXES_KEPT = 4
# A new prefix is kept only when it adds at least this much shared text.
MINIMUM_GAIN = 4096
# Text a checkpoint may not follow: quotes, comments, line splices and directives.
_UNSAFE = ('"', "'", "/*", "//", "\\\n", "#")
_PAIRS = (("(", ")"), ("{", "}"), ("[", "]"))


def common_length(left: str, right: str, low: int = 0) -> int:
    """Length of the common prefix; left[:low] == right[:low] is already known."""
    high = min(len(left), len(right))
    while low < high:
        middle = (low + high + 1) // 2
        if left[:middle] == right[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def _balance(text: str, start: int, end: int) -> tuple[int, int, int]:
    return tuple(text.count(opening, start, end) - text.count(closing, start, end) for opening, closing in _PAIRS)  # type: ignore[return-value]


def checkpoint(text: str, limit: int, start: int = 0) -> int:
    """The last checkpoint of text at or before limit, or start; text[:start] is already checkpointed."""
    for marker in _UNSAFE:
        found = text.find(marker, start, limit)
        if found >= 0:
            limit = found
    depth = _balance(text, start, limit)
    cursor = limit
    found = text.rfind(";\n", start, limit)
    while found >= 0:
        position = found + 2
        rest = _balance(text, position, cursor)
        depth = (depth[0] - rest[0], depth[1] - rest[1], depth[2] - rest[2])
        cursor = position
        if depth == (0, 0, 0):
            return position
        found = text.rfind(";\n", start, found + 1)
    return start


class _Store:
    def __init__(self) -> None:
        self.states: OrderedDict[str, Any] = OrderedDict()
        self.previous = ""

    def longest(self, text: str) -> str:
        """The longest kept prefix of text; every kept prefix of text counts as used."""
        matching = [prefix for prefix in self.states if text.startswith(prefix)]
        for prefix in sorted(matching, key=len):
            self.states.move_to_end(prefix)
        return max(matching, key=len, default="")

    def keep(self, prefix: str, state: Any) -> None:
        self.states[prefix] = state


def resumed(
    kind: str,
    text: str,
    advance: Callable[[S | None, str, int, int], S | None],
    finish: Callable[[S | None, str, int], R],
) -> R:
    """finish(state, text, start) where state covers text[:start] (None and 0 for no prefix).

    advance(state, text, start, end) continues a state from start to the checkpoint end; it returns
    None when the pass cannot stop there, and the prefix is then not kept.
    """
    store = cache.memo("prefixes", kind, _Store, size=cache.memory_size, copy_out=cache.clone)
    best = store.longest(text)
    state = store.states[best] if best else None
    common = common_length(text, store.previous, len(best) if store.previous.startswith(best) else 0)
    store.previous = text
    shared = checkpoint(text, common, len(best)) if common >= len(best) + MINIMUM_GAIN else 0
    if shared >= len(best) + MINIMUM_GAIN and shared * 2 > len(text):
        longer = advance(state, text, len(best), shared)
        if longer is not None:
            best, state = text[:shared], longer
            store.keep(best, state)
    result = finish(state, text, len(best))
    cache.remember("prefixes", kind, store, size=cache.memory_size, copy_out=cache.clone)
    return result


def concatenated(kind: str, text: str, transform: Callable[[str], str]) -> str:
    """transform(text) for a transform that restarts at every checkpoint."""

    def advance(state: str | None, whole: str, start: int, end: int) -> str:
        return (state or "") + transform(whole[start:end])

    def finish(state: str | None, whole: str, start: int) -> str:
        return (state or "") + transform(whole[start:]) if start else transform(whole)

    return resumed(kind, text, advance, finish)


def forget() -> None:
    cache.forget(["prefixes"])


def release_units() -> None:
    """Keep reusable checkpoint contracts, releasing each pass's full previous unit."""
    for kind, store in cache.retained("prefixes"):
        store.previous = max(store.states, key=len, default="")
        cache.remember("prefixes", kind, store, size=cache.memory_size, copy_out=cache.clone)
