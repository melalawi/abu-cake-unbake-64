"""Own callee declarations and measured stack operands for C drafts."""

import hashlib
import re
from pathlib import Path
from typing import Any

from unbake.cdecl import LayoutParser
from unbake.config import Held, Host, Project
from unbake.decomp.draft_context import preprocess_context
from unbake.layout import split
from unbake.layout.structs_types import SCALARS


def declarations(
    project: Project,
    policy: Host,
    version: str,
    assembly: str,
    context: str,
    *,
    function: str | None = None,
    types_path: Path | None = None,
) -> str:
    """Use one callee contract, preferring the whole-program transport proof."""
    from unbake.typemap import types_db
    from unbake.typemap.abi_declarations import for_caller

    callees = set(re.findall(r"\b(?:jal|j)\s+([A-Za-z_]\w*)", assembly))
    records = types_db.entries(types_path, "functions", callees) if types_path is not None else {}
    result: list[str] = []
    for name in sorted(callees):
        record = records.get(name, {})
        carrier = (
            {"prototype": record["prototype"], "reasons": ["types.declaration: solved callee prototype"]}
            if record.get("state") == "known" and record.get("prototype")
            else for_caller(record, function)
        )
        if not carrier.get("prototype"):
            continue
        declaration = carrier["prototype"]
        reasons = list(carrier["reasons"])
        abi = record.get("abi") or {}
        registers = abi.get("registers", [])
        words = [f"r{4 + slot}" if slot < 4 else f"stack{slot * 4}" for slot in range(len(registers))]
        # An old-style C declaration does not tell m2c about stack operands.
        # Specialize only a complete, contiguous, proven O32 word contract;
        # this draft-local transport signature does not settle semantic types.
        if (
            abi.get("arity_known")
            and registers == words
            and not abi.get("missing")
            and not abi.get("conflicts")
            and re.search(r"\b" + re.escape(name) + r"\s*\(\s*\)", declaration)
        ):
            declaration = re.sub(
                r"(\b" + re.escape(name) + r"\s*)\(\s*\)",
                r"\g<1>(" + (", ".join("int" for _ in words) or "void") + ")",
                declaration,
            )
            reasons.append("types.abi.draft_words: complete callee entry reads, including consumed stack operands")
        reason = "; ".join(reasons).replace("*/", "* /")
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
        parser = LayoutParser(clean[: matches[0].start()])
        try:
            while parser.peek():
                if parser.peek() == "typedef":
                    parser.take()
                    parser.declaration(typedef=True)
                else:
                    parser.skip_external()
        except (Held, ValueError):
            continue

        def abi_type(value: str, *, parameter: bool, parser: LayoutParser = parser) -> str | None:
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


def mapped_body(project: Project, function: str, version: str) -> dict[str, Any] | None:
    """Use a draft snapshot only when its selected caller bytes are unchanged."""
    from unbake.typemap import shards
    from unbake.typemap.mapping import load_map

    mapped = load_map(project, allow_stale=True)
    functions = mapped["functions"]
    inventory = getattr(functions, "inventory", functions)
    name = (
        function
        if function in inventory
        else next((name for name, item in inventory.items() if function in item["aliases"]), None)
    )
    if name is None or version not in inventory[name]["versions"]:
        return None
    # Entry evidence comes only from this containing body. Unrelated entries
    # (including compiler runtime symbols) cannot establish or block its ABI.
    body: dict[str, Any] = (
        functions.version(name, version)
        if isinstance(functions, shards.Functions)
        else functions[name]["versions"][version]
    )
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1 or hashlib.sha256(split.words(project, rows[0])).hexdigest() != body.get("target_sha256"):
        raise Held("m2c", f"{function}: types.abi.target_stale: caller bytes changed; run unbake recompute types")
    return body


def stack_arguments(source: str, context: str, function: str, body: dict[str, Any]) -> str:
    """Recover a lost stack operand only when every mapped call proves one value."""
    from unbake.config import Held
    from unbake.decomp.draft_macros import calls
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
