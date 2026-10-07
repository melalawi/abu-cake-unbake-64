"""Lower decompiler placeholders with balanced C argument boundaries."""

import re
from collections.abc import Callable

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.config import Held
from unbake.decomp.draft_context import _typedefs
from unbake.process import named as cause_named


def calls(source: str, name: str, replace: Callable[[list[str]], str]) -> str:
    """Rewrite nested calls while leaving comments and string literals intact."""
    tokens = list(cdecl.SOURCE_TOKEN.finditer(source))
    edits = []
    for index, token in enumerate(tokens):
        if token[0] != name or index + 1 == len(tokens) or tokens[index + 1][0] != "(":
            continue
        depth = 0
        start = tokens[index + 1].end()
        args = []
        for tail in tokens[index + 2 :]:
            word = tail[0]
            if word in ("(", "[", "{"):
                depth += 1
            elif word in (")", "]", "}"):
                if depth == 0:
                    args.append(source[start : tail.start()].strip())
                    edits.append((token.start(), tail.end(), args))
                    break
                depth -= 1
            elif word == "," and depth == 0:
                args.append(source[start : tail.start()].strip())
                start = tail.end()
        else:
            line = source.count("\n", 0, token.start()) + 1
            site = source[token.start() : token.end() + 80]
            raise Held(
                cause_named(
                    "decomp.draft_macros.calls",
                    f"unresolved {name} at line {line}: {site}",
                    owner="decomp.draft_macros",
                    stage="m2c",
                )
            )
    # Rewrite the outer call recursively so nested replacements never overlap.
    cursor = 0
    result: list[str] = []
    for start, end, args in edits:
        if start < cursor:
            continue
        result.extend((source[cursor:start], replace([calls(arg, name, replace) for arg in args])))
        cursor = end
    return "".join(result) + source[cursor:]


def lower(source: str, context: str, *, allow_fields: bool = False) -> str:
    """Use declared unknown scalar types and preserve lvalue bit reinterpretation."""
    # m2c spells null pointer constants as NULL even when the project has
    # no such macro. Integer zero needs no declaration in a C draft.
    source = cdecl.SOURCE_TOKEN.sub(lambda token: "0" if token[0] == "NULL" else token[0], source)
    incoming = re.search(r"\bsaved_reg_([A-Za-z0-9]+)\b", re.sub(r"/\*.*?\*/|//[^\n]*", " ", source, flags=re.S))
    if incoming:
        raise Held(
            cause_named(
                "decomp.draft_macros.lower",
                f"incoming saved register ${incoming[1]} has no declared C parameter or dominating definition",
                owner="decomp.draft_macros",
                stage="m2c",
            )
        )

    helpers: dict[str, str] = {}

    def bitwise(args: list[str]) -> str:
        if len(args) != 2 or not re.fullmatch(r"[A-Za-z_]\w*(?:\s*\*)*", args[0]):
            raise Held(
                cause_named(
                    "decomp.draft_macros.bitwise",
                    "unresolved M2C_BITWISE(" + ", ".join(args) + ")",
                    owner="decomp.draft_macros",
                    stage="m2c",
                )
            )
        target, value = args
        typedefs = "\n".join(f"typedef int {name};" for name in sorted(_typedefs(context)))
        expression = None
        try:
            tree = cdecl.parse(typedefs + "\nvoid __m2c_value(void) { " + value + "; }")
            expression = tree.ext[-1].body.block_items[0]
            addressable = isinstance(expression, (c_ast.ID, c_ast.ArrayRef, c_ast.StructRef)) or (
                isinstance(expression, c_ast.UnaryOp) and expression.op == "*"
            )
        except (cdecl.ParseError, AttributeError, IndexError):
            addressable = False
        if not addressable:
            if (
                target in ("s32", "u32", "int", "unsigned int")
                and isinstance(expression, c_ast.FuncCall)
                and isinstance(expression.name, c_ast.ID)
            ):
                signature = re.search(
                    r"\b(?:f32|float)\s+" + re.escape(expression.name.name) + r"\s*\([^;{}]*\)\s*;",
                    context,
                )
                if signature:
                    helper = "m2c_float_to_word"
                    while re.search(r"\b" + helper + r"\b", source + context):
                        helper += "_"
                    helpers[helper] = (
                        f"static {target} {helper}(float value) {{\n"
                        "    union { unsigned int bits; float value; } word;\n"
                        "    word.value = value;\n    return word.bits;\n}\n"
                    )
                    return f"{helper}({value})"
            if (
                target in ("f32", "float")
                and isinstance(expression, c_ast.FuncCall)
                and isinstance(expression.name, c_ast.ID)
            ):
                signature = re.search(
                    r"\b(?:s32|u32|int|signed int|unsigned int|long|unsigned long)\s+"
                    + re.escape(expression.name.name)
                    + r"\s*\([^;{}]*\)\s*;",
                    context,
                )
                if signature:
                    helper = "m2c_bits_to_" + target
                    while re.search(r"\b" + helper + r"\b", source + context):
                        helper += "_"
                    helpers[helper] = (
                        f"static {target} {helper}(unsigned int bits) {{\n"
                        f"    union {{ unsigned int bits; {target} value; }} word;\n"
                        "    word.bits = bits;\n    return word.value;\n}\n"
                    )
                    return f"{helper}({value})"
            raise Held(
                cause_named(
                    "decomp.draft_macros.bitwise",
                    "unresolved M2C_BITWISE(" + ", ".join(args) + "): requires addressable value",
                    owner="decomp.draft_macros",
                    stage="m2c",
                )
            )
        return f"(*(({target} *)&({value})))"

    if not allow_fields:
        # A field placeholder is an lvalue only after share lowers it.
        source = calls(source, "M2C_BITWISE", bitwise)
    known = set(re.findall(r"\btypedef\b[^;]*\b(M2C_UNK\d*)\s*;", context + "\n" + source))
    for token in cdecl.SOURCE_TOKEN.finditer(source):
        if re.fullmatch(r"M2C_\w+", token[0]) and token[0] not in known:
            if allow_fields and token[0] in ("M2C_FIELD", "M2C_BITWISE"):
                continue
            line = source.count("\n", 0, token.start()) + 1
            raise Held(
                cause_named(
                    "decomp.draft_macros.lower",
                    f"unresolved {token[0]} at line {line}: {source.splitlines()[line - 1].strip()}",
                    owner="decomp.draft_macros",
                    stage="m2c",
                )
            )
    return "".join(helpers.values()) + source
