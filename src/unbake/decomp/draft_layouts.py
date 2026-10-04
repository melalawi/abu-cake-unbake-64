"""Resolve decompiler unknowns before they cross the shared-header boundary."""

import re

from unbake.cdecl import SOURCE_TOKEN, LayoutParser
from unbake.config import Held
from unbake.decomp import opaque_pointers
from unbake.layout.structs_types import SCALARS


def normalize(source: str, context: str) -> str:
    """Replace only unknowns whose target ABI width is established by a typedef.

    Scalar spelling also makes pointers and arrays self-contained in a shared
    header, independently of whether its consumers include m2c's prelude.
    Unknown type names without layout evidence are refused before any write.
    """
    source = opaque_pointers.normalize(source, context)
    parser = LayoutParser(context + "\n" + source)
    # Collect typedefs without evaluating aggregates: a member can itself be
    # the unknown that this pass is about to resolve.
    while parser.peek():
        if parser.peek() == "typedef":
            parser.take()
            parser.declaration(typedef=True)
        else:
            parser.skip_external()
    replacements = []
    for token in SOURCE_TOKEN.finditer(source):
        name = token[0]
        if not re.fullmatch(r"M2C_UNK\d*", name):
            continue
        if name not in parser.types:
            raise Held("m2c", f"{name}: unknown type has no declared target layout")
        spelling = parser.type_name(name, ())
        if spelling not in SCALARS or spelling in ("float", "double", "f32", "f64"):
            raise Held("m2c", f"{name}: unknown type has unsupported target layout {spelling}")
        width = SCALARS[spelling][0]
        replacements.append((token.start(), token.end(), f"s{width * 8}"))
    for start, end, value in reversed(replacements):
        source = source[:start] + value + source[end:]
    return source
