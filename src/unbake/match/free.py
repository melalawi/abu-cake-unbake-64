"""Internal batch selection requiring full current trial proof for every owner."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

from unbake.match import queue
from unbake.project.config import Held, Policy, Project


def land(
    project: Project,
    policy: Policy,
    version: str,
    *,
    report: Callable[[str], None] | None = None,
) -> list[str]:
    """Try each partial, reporting decisions immediately, then publish together."""
    started = perf_counter()
    messages: list[str] = []

    def emit(message: str) -> None:
        messages.append(message)
        if report is not None:
            report(message)

    text = project.version(version).split.read_text()
    assembly = {entry[1] for line in text.splitlines() if (entry := queue.row(line)) and entry[0] == "asm"}
    sources = [source for source in sorted(project.src.glob("*.c")) if source.stem in assembly]
    emit(f"OK(match): scan VERSION {version}: {len(sources)} eligible; wall={perf_counter() - started:.3f}s")
    queued = skipped = refused = matched = 0
    try:
        for source in sources:
            step = perf_counter()
            try:
                decisions = queue.submit(project, policy, source)
                queued += 1
                for decision in decisions:
                    emit(f"{decision}; step=submit wall={perf_counter() - step:.3f}s")
            except Held as error:
                if error.phase in ("match", "submit") and "requires identical_everywhere=true" in error.reason:
                    skipped += 1
                    emit(
                        f"OK(match): skipped {source.stem}: refused: {error.reason}; "
                        f"step=submit wall={perf_counter() - step:.3f}s"
                    )
                else:
                    refused += 1
                    emit(f"HELD(match): {source.stem}: {error.reason}; step=submit wall={perf_counter() - step:.3f}s")
        step = perf_counter()
        pending = queue.status(project=project, policy=policy)
        emit(f"OK(match): queue status: {len(pending)} pending; wall={perf_counter() - step:.3f}s")
        if pending:
            emit("OK(match): publication started (staging/build/publish)")
            step = perf_counter()
            try:
                for decision in queue.run(project, policy):
                    matched += int(decision.startswith("OK(match):") and " matched on VERSION " in decision)
                    refused += int(decision.startswith("HELD("))
                    emit(decision)
            finally:
                emit(f"OK(match): publication finished; wall={perf_counter() - step:.3f}s")
    finally:
        emit(
            f"OK(match): summary VERSION {version}: eligible={len(sources)} queued={queued} "
            f"accepted={matched} skipped={skipped} refused={refused}; wall={perf_counter() - started:.3f}s"
        )
    return messages
