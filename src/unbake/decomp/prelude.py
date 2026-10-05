"""Resolve m2c's prelude typedefs in a published source: each M2C_UNK alias becomes the shared type it names."""

from __future__ import annotations

import re

_TYPEDEF = re.compile(r"^[ \t]*typedef[ \t]+(\w+)[ \t]+(M2C_UNK\w*)[ \t]*;[ \t]*\n", re.M)
_USE = re.compile(r"\bM2C_UNK\w*\b")


def resolve(text: str) -> str:
    """TEXT without its `typedef TYPE M2C_UNK*;` lines, every use spelled as the TYPE the typedef names."""
    names = {match[2]: match[1] for match in _TYPEDEF.finditer(text)}
    if not names:
        return text
    text = _TYPEDEF.sub("", text)
    text = _USE.sub(lambda match: names.get(match[0], match[0]), text)
    return re.sub(r"\n{3,}", "\n\n", text)
