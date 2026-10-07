"""Resolve m2c's prelude typedefs in a published source: each M2C_UNK alias becomes the shared type it names."""

from __future__ import annotations

import re

from unbake.process import named as cause_named

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


def fields(text: str) -> str:
    """Canonicalize local scalar offset helpers before shared field lowering.

    Only the measured dereference shape is accepted. Other macros retain their
    meaning and are left for the source guard to refuse.
    """
    from unbake.decomp.draft_macros import calls

    helpers = {}
    definition = re.compile(r"^\s*#\s*define\s+(\w+)\(([^\n)]*)\)\s+([^\n]+)$", re.M)
    for match in definition.finditer(text):
        params = [value.strip() for value in match[2].split(",")]
        if len(params) != 3 or any(not re.fullmatch(r"[A-Za-z_]\w*", param) for param in params):
            continue
        body = re.sub(r"\s+", "", match[3])
        for base, type_, offset in ((0, 1, 2), (1, 0, 2)):
            b, t, o = params[base], params[type_], params[offset]
            for byte in ("char", "s8", "u8"):
                for pointer in (False, True):
                    cast = t if pointer else t + "*"
                    if body == f"(*({cast})(({byte}*)({b})+({o})))":
                        helpers[match[1]] = (base, type_, offset, pointer)
    if not helpers:
        return text
    text = definition.sub(lambda match: "" if match[1] in helpers else match[0], text)
    for name, (base, type_, offset, pointer) in helpers.items():

        def canonical(
            args: list[str],
            name: str = name,
            base: int = base,
            type_: int = type_,
            offset: int = offset,
            pointer: bool = pointer,
        ) -> str:
            if len(args) != 3:
                from unbake.config import Held

                raise Held(
                    cause_named(
                        "decomp.prelude.canonical",
                        f"unresolved {name}(" + ", ".join(args) + ")",
                        owner="decomp.prelude",
                        stage="draft",
                    )
                )
            type_name = args[type_] if pointer else args[type_] + " *"
            return f"M2C_FIELD({args[base]}, {type_name}, {args[offset]})"

        text = calls(text, name, canonical)
    return text
