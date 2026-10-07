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
from unbake.process import named as cause_named


def declared_void_exit(
    record: dict[str, Any], function: str, version: str, body: dict[str, Any], callee: dict[str, Any]
) -> bool:
    """Reuse a declared void entry whose incidental exits come from a proved void call.

    This proves no absence of physical result-register writes and changes no
    semantic or machine record. The two authorities are the existing C return
    contract and the last callee's own proved definition, not the candidate.
    """
    from unbake import cdecl
    from unbake.fold.callee_contracts import _signature
    from unbake.typemap import evidence, o32

    abi = record.get("abi") or {}
    returned = record.get("return") or {}
    authority = returned.get("provenance", [])
    if isinstance(authority, dict):
        authority = [authority]
    if (
        returned.get("state") != "known"
        or returned.get("type") != "void"
        or not any(row.get("kind") in ("published", "proven", "declared") for row in authority)
        or not abi.get("arity_known")
        or abi.get("missing")
        or abi.get("conflicts")
        or abi.get("used_returns")
        or abi.get("unproven_return_reads")
        or any(abi.get("caller_return_uses", {}).values())
        or abi.get("return_width") == 8
        or not isinstance(body, dict)
        or not body.get("target_sha256")
        or body.get("unknown")
        or body.get("unknown_control")
        or not body.get("returns")
        or not body.get("calls")
        or any(call.get("tail") for call in body["calls"])
    ):
        return False
    inputs = abi.get("inputs", {})
    versions = set(record.get("versions", inputs))
    consumed = set(inputs.get(version, []))
    actual = {reg for reg in body.get("register_inputs", []) if evidence.argument(reg)}
    if (
        not versions
        or set(inputs) != versions
        or version not in versions
        or any(set(row) != consumed for row in inputs.values())
        or set(abi.get("registers", [])) != consumed
        or actual != consumed
    ):
        return False
    carrier = record.get("abi_declaration", {}).get("prototype")
    signature = _signature(carrier, {}) if carrier else None
    if (
        signature is None
        or cdecl.declarations(carrier).declared != {function}
        or signature["return"] != "void"
        or not signature["arity_known"]
        or signature["variadic"]
        or any(
            p["type"] not in ("int", "unsigned int", "long", "unsigned long") and not p["type"].endswith(" *")
            for p in signature["params"]
        )
        or o32.argument_words(signature, {}) != consumed
    ):
        return False
    last = max(body["calls"], key=lambda call: call["instruction"])
    name = last.get("callee")
    provenance = callee.get("provenance", [])
    if isinstance(provenance, dict):
        provenance = [provenance]
    prototype = callee.get("prototype")
    promised = _signature(prototype, {}) if prototype else None
    callee_abi = callee.get("abi") or {}
    if (
        not name
        or callee.get("state") != "known"
        or not any(row.get("kind") == "proven" and row.get("function") == name for row in provenance)
        or version not in callee.get("versions", [])
        or not prototype
        or promised is None
        or cdecl.declarations(prototype).declared != {name}
        or promised["return"] != "void"
        or not promised["arity_known"]
        or promised["variadic"]
        or not callee_abi.get("arity_known")
        or callee_abi.get("missing")
        or callee_abi.get("conflicts")
        or callee_abi.get("used_returns")
        or callee_abi.get("unproven_return_reads")
        or any(callee_abi.get("caller_return_uses", {}).values())
        or callee_abi.get("return_width") == 8
        or any(last.get("return_register_use", {}).values())
    ):
        return False
    index = (last["instruction"] - body["address"]) // 4
    for exit_ in body["returns"]:
        if exit_["instruction"] <= last["instruction"]:
            return False
        for reg in ("r2", "f0"):
            origin = f"return:{function}:{version}:{index}:{reg}"
            value = exit_["values"].get(reg, {})
            if (
                value.get("origins") != [{"id": origin, "offset": 0}]
                or value.get("constant") is not None
                or set(value.get("dependencies", [])) - {origin}
            ):
                return False
    return True


def leaf_entry_record(record: dict[str, Any], function: str, version: str, body: dict[str, Any]) -> dict[str, Any]:
    """Reconcile an old caller-union carrier against byte-pinned leaf reads.

    The installed semantic record is untouched. Typed C contracts, forwarding,
    incomplete control, version differences and unproved exits remain strict.
    """
    from unbake.typemap import abi_declarations, evidence

    if not isinstance(body, dict) or not body.get("target_sha256"):
        return record
    abi = record.get("abi") or {}
    provenance = record.get("provenance", [])
    if isinstance(provenance, dict):
        provenance = [provenance]
    if (
        record.get("prototype")
        or any(
            row.get("kind") not in (None, "machine", "published")
            or (row.get("kind") == "published" and row.get("function") in (None, function))
            for row in provenance
        )
        or body.get("calls")
        or body.get("unknown")
        or abi.get("conflicts")
        or not abi.get("return_known")
        or abi.get("return_width") == 8
    ):
        return record
    inputs = abi.get("inputs", {})
    versions = set(record.get("versions", inputs))
    if not versions or set(inputs) != versions or version not in inputs:
        return record
    consumed = set(inputs[version])
    if any(set(row) != consumed for row in inputs.values()):
        return record
    actual = {reg for reg in body.get("register_inputs", []) if evidence.argument(reg)}
    parameters = {param["register"] for param in record.get("params", [])}
    old = set(abi.get("registers", []))
    if not consumed < old or actual != consumed or parameters != consumed:
        return record
    if any(param.get("type") in ("float", "double", "long long", "unsigned long long") for param in record["params"]):
        return record
    if record.get("return", {}).get("type") in ("float", "double", "long long", "unsigned long long"):
        return record
    missing = [row for row in abi.get("missing", []) if row.get("register") in consumed]
    if missing:
        return record
    returned = abi.get("return_register")
    if returned not in (None, "r2") or abi.get("unproven_return_reads"):
        return record
    if returned == "r2":
        if "r2" not in body.get("register_outputs", []) or not body.get("returns"):
            return record
        for exit_ in body["returns"]:
            value = exit_["values"].get("r2", {})
            if not value.get("defined", not value.get("unknown", True)) or value.get("origins") == [
                {"id": f"param:{function}:r2", "offset": 0}
            ]:
                return record
    result = {
        **record,
        "abi": {**abi, "registers": sorted(consumed), "arity_known": True, "missing": missing},
        "entry_reconciliation": {
            "kind": "legacy caller-union leaf carrier",
            "version": version,
            "consumed_registers": sorted(consumed),
            "caller_only_registers": sorted(old - consumed),
            "target_sha256": body.get("target_sha256"),
            "resolution": "byte-pinned leaf entry; semantic database retained",
        },
    }
    result["abi_declaration"] = abi_declarations.prototype(function, result, {})
    return result


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
        raise Held(
            cause_named(
                f"{function}",
                f"{function}: types.abi.target_stale: caller bytes changed; run unbake recompute types",
                owner="decomp.draft_abi",
                stage="m2c",
            )
        )
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
