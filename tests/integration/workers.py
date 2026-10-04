"""Module-level worker functions for pool tests (workers import this module by name)."""

import os
from pathlib import Path


def double(value: int) -> int:
    return value * 2


def crash_once(item: tuple[str, int]) -> int:
    marker, value = item
    if value < 0 and not Path(marker).exists():
        Path(marker).write_text("crashed")
        os._exit(7)
    return abs(value)


def crash_always(item: int) -> int:
    os._exit(7)
