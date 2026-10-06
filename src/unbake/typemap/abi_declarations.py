"""C transport declarations for proven register ABIs with unknown semantics."""

from __future__ import annotations

from typing import Any

from unbake.typemap import declarations, evidence


def prototype(name: str, record: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """Do not turn a register carrier into a solved semantic signature."""
    abi = record.get("abi")
    if not abi:
        return {"prototype": None, "reasons": ["types.abi.absent: machine ABI evidence is absent"]}
    reasons = []
    params = {p["register"]: p for p in record["params"]}
    types = {reg: param.get("type") for reg, param in params.items()}
    ordered = evidence.parameters(abi["registers"], types)
    if ordered is None and not any(reg.startswith("f") for reg in abi["registers"]):
        slots = [int(reg[5:]) // 4 if reg.startswith("stack") else int(reg[1:]) - 4 for reg in abi["registers"]]
        supplied = set(abi["registers"]) | set(abi.get("argument_slots", []))
        count = max(slots, default=-1) + 1
        # An unproved distant stack slot cannot justify filling the interval.
        # Establish its cardinality from finite evidence before materializing it.
        if count <= len(supplied):
            required = [f"r{4 + slot}" if slot < 4 else f"stack{slot * 4}" for slot in range(count)]
            holes = set(required) - set(abi["registers"])
            if holes <= set(abi.get("argument_slots", [])):
                for reg in sorted(holes):
                    params[reg] = {"state": "unknown", "type": None}
                    types[reg] = None
                    reasons.append(
                        f"types.abi.unused_slot: {reg}: every mapped caller supplies the intervening O32 word"
                    )
                ordered = evidence.parameters(required, types)
    unspecified = ordered is None
    if unspecified:
        reasons.append("types.abi.slots: parameter slots unresolved; declaration leaves the argument list unspecified")
    carriers = []
    for reg in ordered or []:
        param = params.get(reg, {"state": "unknown", "type": None})
        type_ = types.get(reg)
        if type_ is not None:
            type_ = declarations.canonical(type_, aliases)
        if reg.startswith("f"):
            if type_ not in ("float", "double"):
                unspecified = True
                reasons.append(f"types.abi.fp_width: {reg}: floating representation unknown; argument list unspecified")
                break
        elif type_ is None or declarations.unknown(type_):
            type_ = "int"
            reasons.append(f"types.abi.word: {reg}: semantic type {param['state']}; one O32 word carrier")
        carriers.append(type_)
    if not unspecified:
        expected = declarations.parameter_registers([{"type": t} for t in carriers], aliases)
        if expected != ordered:
            for i, (reg, expected_reg) in enumerate(zip(ordered or [], expected, strict=True)):
                if reg != expected_reg and reg.startswith("r") and carriers[i] == "float":
                    carriers[i] = "int"
                    reasons.append(f"types.abi.bits: {reg}: floating bits carried in one O32 word")
            if declarations.parameter_registers([{"type": t} for t in carriers], aliases) != ordered:
                unspecified = True
                reasons.append("types.abi.parameters: C argument convention unresolved; argument list unspecified")
    if not unspecified and not carriers and abi.get("caller_arguments"):
        # The callee reads no argument, but a caller passes one (a K&R call): (void) would refuse that call.
        unspecified = True
        reasons.append("types.abi.caller_arguments: callers pass arguments the callee never reads; list unspecified")
    if abi["missing"]:
        reasons.append("types.abi.arguments: some mapped callers do not establish every consumed argument value")
    if any("input registers differ" in reason for reason in abi["conflicts"]):
        unspecified = True
        reasons.append(
            "types.abi.versions: callee argument registers differ across versions; argument list unspecified"
        )
    used = abi.get("used_returns", [])
    if not used and not abi.get("call_sites") and len(abi.get("defined_returns", [])) > 1:
        return {
            "prototype": None,
            "reasons": ["types.abi.return: unconsumed integer and floating exit values are ambiguous"],
        }
    returned = record["return"].get("type")
    if returned is not None:
        returned = declarations.canonical(returned, aliases)
    if abi.get("unproven_return_reads"):
        reasons.append(
            "types.abi.stale_return_read: caller reads of registers absent at callee exits are not return ABI proof"
        )
    if len(used) > 1:
        return {"prototype": None, "reasons": ["types.abi.return: callers consume incompatible return registers"]}
    proven_return = (
        record["return"].get("state") == "known"
        and returned is not None
        and abi.get("machine_return_known", abi["return_known"])
        and ((abi.get("return_register") == "f0") == (returned in ("float", "double")))
    )
    observed_uses = {reg for uses in abi.get("caller_return_uses", {}).values() for reg in uses}
    if not used and not observed_uses and abi.get("call_sites", 0) and not proven_return:
        # The direct-call contract has no observable result. Preserve the
        # unknown function signature in the DB; this is only a call carrier.
        returned = "void"
        reasons.append("types.abi.unused_return: no mapped direct caller consumes a result; semantic return unknown")
    elif abi["void"]:
        returned = "void"
    elif (abi["return_register"] in (None, "r2")) and (
        abi.get("machine_return_known", abi["return_known"]) or abi.get("word_return_written")
    ):
        if not abi.get("machine_return_known", abi["return_known"]):
            reasons.append(
                "types.abi.result_value_unknown: callee writes v0 at its exits; forwarded value remains unproven"
            )
        if returned is None or returned in ("float", "double") or declarations.unknown(returned):
            returned = "int"
            reasons.append("types.abi.word_return: v0 carries one O32 word; semantic return unknown or conflicting")
    elif abi["return_register"] == "f0" and abi.get("machine_return_known", abi["return_known"]):
        if returned not in ("float", "double"):
            return {"prototype": None, "reasons": ["types.abi.fp_return: floating return width is unknown"]}
    else:
        return {"prototype": None, "reasons": ["types.abi.return: consumed result is not proven at every callee exit"]}
    return {
        "prototype": declarations.declarator(
            returned,
            name
            + "("
            + (
                ""
                if unspecified
                else ", ".join(declarations.declarator(type_, "").strip() for type_ in carriers) or "void"
            )
            + ")",
        )
        + ";",
        "parameters_known": not unspecified,
        "reasons": reasons or ["types.abi.transport: register ABI proven; semantic signature remains unresolved"],
    }


def for_caller(record: dict[str, Any], function: str | None) -> dict[str, Any]:
    """Select a proven register contract without settling a global conflict."""
    carrier = record.get("abi_declaration", {})
    if carrier.get("prototype") or function is None:
        return dict(carrier)
    callers = (record.get("abi") or {}).get("caller_return_uses", {})
    uses = callers.get(function, [])
    if len(uses) == 1:
        return dict(carrier.get("variants", {}).get(uses[0], carrier))
    if function in callers and not uses:
        return dict(carrier.get("variants", {}).get("unused", carrier))
    return dict(carrier)
