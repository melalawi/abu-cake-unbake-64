"""Own callee declarations and measured stack operands for C drafts."""

import re
from typing import Any

from unbake.decomp.draft_context import preprocess_context
from unbake.layout import split
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import SCALARS
from unbake.project.config import Held, Policy, Project


def declarations(
    project: Project,
    policy: Policy,
    version: str,
    assembly: str,
    context: str,
    *,
    function: str | None = None,
    database: dict[str, Any] | None = None,
) -> str:
    """Use one callee contract, preferring the whole-program transport proof."""
    from unbake.typemap.abi_declarations import for_caller

    callees = set(re.findall(r"\b(?:jal|j)\s+([A-Za-z_]\w*)", assembly))
    result: list[str] = []
    for name in sorted(callees):
        record = database["functions"].get(name, {}) if database is not None else {}
        carrier = for_caller(record, function)
        if not carrier.get("prototype"):
            continue
        reason = "; ".join(carrier["reasons"]).replace("*/", "* /")
        declaration = carrier["prototype"]
        if not declaration.startswith(("extern ", "static ")):
            declaration = "extern " + declaration
        result.extend((f"/* {name}: {reason} */", declaration))
        callees.remove(name)
    rows = split.functions(project, version)
    for name in sorted(callees):
        if re.search(r"\b" + re.escape(name) + r"\s*\(", context):
            continue
        owners = [row for row in rows if name in row.aliases]
        if len(owners) != 1:
            continue
        source = project.src / (owners[0].path + ".c")
        if not source.is_file():
            continue
        # Select the same VERSION and include graph as the actual definition.
        try:
            expanded = preprocess_context(source, project, policy, version, source.stem)
        except Held:
            # An unavailable private include cannot invalidate an otherwise
            # supported caller. Its inferred signature still faces cc1 proof.
            continue
        clean = re.sub(r"/\*.*?\*/|//[^\n]*", " ", expanded, flags=re.S)
        pattern = (
            r"(?m)^[ \t]*(?P<return>(?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*\s*\**\s*)"
            + re.escape(name)
            + r"\s*\((?P<args>[^()]*)\)\s*\{"
        )
        matches = list(re.finditer(pattern, clean))
        if len(matches) != 1:
            continue
        parser = Parser(clean[: matches[0].start()])
        try:
            while parser.peek():
                if parser.peek() == "typedef":
                    parser.take()
                    parser.declaration(typedef=True)
                else:
                    parser.skip_external()
        except (Held, ValueError):
            continue

        def abi_type(value: str, *, parameter: bool, parser: Parser = parser) -> str | None:
            value = re.sub(r"\b(?:const|volatile|restrict|extern|static|inline)\b", "", value).strip()
            if parameter:
                value = re.sub(r"\b[A-Za-z_]\w*\s*(?:\[[^]]*\]\s*)*$", "", value).strip()
            if "*" in value or "[" in value:
                # Private aggregate names cannot escape their defining unit.
                # Keep known scalar pointees, otherwise only the pointer ABI.
                base = value.split("*", 1)[0].strip()
                if base not in SCALARS and base != "void":
                    base = "void"
                return base + " " + "*" * max(1, value.count("*"))
            if value == "void" or value in SCALARS:
                return value
            if value in parser.types:
                resolved = parser.type_name(value, ())
                return resolved if resolved in SCALARS else None
            return None

        match = matches[0]
        returned = abi_type(match["return"], parameter=False)
        args = match["args"].strip()
        parameters = [] if args in ("", "void") else args.split(",")
        types = ["..." if arg.strip() == "..." else abi_type(arg, parameter=True) for arg in parameters]
        if returned is not None and all(value is not None for value in types):
            result.append(f"{returned} {name}({', '.join(value for value in types if value is not None) or 'void'});")
    return "\n".join(result)


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
