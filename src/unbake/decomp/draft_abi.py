"""Keep unresolved semantic signatures visible in draft callee declarations."""

import re
from typing import Any


def callees(source: str, function: str, database: dict[str, Any]) -> str:
    """Reuse only measured transport declarations for names the draft calls."""
    from unbake.typemap.abi_declarations import for_caller

    code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", source, flags=re.S)
    used = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", code)) - {function}
    lines: list[str] = []
    for name in sorted(used):
        record = database["functions"].get(name, {})
        carrier = for_caller(record, function)
        if not carrier.get("prototype"):
            continue
        reason = "; ".join(carrier["reasons"]).replace("*/", "* /")
        declaration = carrier["prototype"]
        if not declaration.startswith(("extern ", "static ")):
            declaration = "extern " + declaration
        lines.extend((f"/* {name}: {reason} */", declaration))
    return "\n".join(lines) + "\n" if lines else ""


def stack_arguments(source: str, context: str, function: str, body: dict[str, Any]) -> str:
    """Recover a lost stack operand only when every mapped call proves one value."""
    from unbake.decomp.draft_macros import calls
    from unbake.project.config import Held
    from unbake.typemap import declarations

    if "Unable to find stack arg" not in source:
        return source
    own = re.search(r"[^;{}\n]+\b" + re.escape(function) + r"\s*\([^{};]*\)\s*\{", source)
    if own is None:
        return source
    text = own[0][:-1] + ";"
    unknowns = set(re.findall(r"\bM2C_UNK\d*\b", text))
    try:
        seed = declarations.extract(
            context + "\n" + "\n".join(f"typedef int {name};" for name in sorted(unknowns)) + "\n" + text, {}
        )
    except Held:
        return source
    signature = seed["functions"].get(function)
    if not signature or not signature["arity_known"]:
        return source
    formals = {reg: p["name"] for reg, p in zip(signature["registers"], signature["params"], strict=True)}
    mapped: dict[str, list[dict[str, Any]]] = {}
    for call in body["calls"]:
        if call["callee"]:
            mapped.setdefault(call["callee"], []).append(call)
    reasons = []
    pattern = re.compile(r"M2C_ERROR\s*\(\s*/\* Unable to find stack arg (0x[0-9a-fA-F]+) in block \*/\s*\)")
    for callee, sites in mapped.items():

        def replace(arguments: list[str], sites: list[dict[str, Any]] = sites, callee: str = callee) -> str:
            def operand(match: re.Match[str]) -> str:
                register = "stack" + str(int(match[1], 16))
                expressions = set()
                for site in sites:
                    value = site["arguments"].get(register, {})
                    origins = value.get("origins", [])
                    constant = value.get("constant")
                    if constant is not None:
                        expressions.add(f"0x{constant:X}")
                    elif len(origins) == 1 and origins[0]["offset"] == 0:
                        origin = origins[0]["id"]
                        owner = sites[0].get("function", function)
                        prefix = f"param:{owner}"
                        if origin.startswith(prefix + ":") and origin[len(prefix) + 1 :] in formals:
                            expressions.add(formals[origin[len(prefix) + 1 :]])
                        else:
                            return match[0]
                    else:
                        return match[0]
                if len(expressions) != 1:
                    return match[0]
                reasons.append(
                    f"types.abi.stack_argument: {callee}: {register}: all mapped call sites prove the operand"
                )
                return next(iter(expressions))

            return callee + "(" + ", ".join(pattern.sub(operand, arg) for arg in arguments) + ")"

        source = calls(source, callee, replace)
    if reasons:
        source = "\n".join("/* " + reason + " */" for reason in sorted(set(reasons))) + "\n" + source
    return source
