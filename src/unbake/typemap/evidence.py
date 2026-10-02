"""Machine observations used by the type constraint solver."""

from __future__ import annotations

from typing import Any

ARGUMENTS = ("r4", "r5", "r6", "r7", "f12", "f14")


def argument(register: str) -> bool:
    return register in ARGUMENTS or (
        register.startswith("stack") and register[5:].isdigit() and int(register[5:]) >= 16
    )


def scalar(access: dict[str, Any]) -> str | None:
    """Storage representations; partial transfers do not establish a scalar."""
    if access.get("partial"):
        return None
    opcode, width, sign = access["opcode"], access["width"], access["signedness"]
    if opcode in (0x31, 0x39):
        return "float"
    if opcode in (0x35, 0x3D):
        return "double"
    if sign is not None:
        return {
            (1, True): "signed char",
            (1, False): "unsigned char",
            (2, True): "short",
            (2, False): "unsigned short",
            (4, True): "int",
            (4, False): "unsigned int",
        }.get((width, sign))
    if width == 4 and access.get("value", {}).get("constant") is not None:
        return "int"
    return None


def abi(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Forwarded entry arguments count as inputs, including tail calls."""
    # Callee entry reads bound possible stack argument slots. Repeated ABI
    # passes need entry forwarding and return liveness, not complete spill maps.
    inputs = {
        (name, version): {reg for reg in body["register_inputs"] if argument(reg)}
        for name, item in facts["functions"].items()
        for version, body in item["versions"].items()
    }
    slots = set(ARGUMENTS) | {reg for regs in inputs.values() for reg in regs}
    summaries = {}
    for name, item in facts["functions"].items():
        versions = {}
        for version, body in item["versions"].items():
            calls_ = []
            for call in body["calls"]:
                if call["callee"] is None:
                    continue
                calls_.append(
                    {
                        **{key: call[key] for key in ("callee", "function", "version", "instruction")},
                        "tail": call.get("tail", False),
                        "arguments": {
                            reg: {
                                "unknown": value.get("unknown", True),
                                "origins": [
                                    origin
                                    for origin in value.get("origins", [])
                                    if origin["id"].startswith(f"param:{name}:")
                                ],
                            }
                            for reg, value in call["arguments"].items()
                            if reg in slots
                        },
                        "return_register_use": {
                            reg: bool(uses)
                            for reg, uses in call.get("return_register_use", {}).items()
                            if reg in ("r2", "f0")
                        },
                    }
                )
            versions[version] = {
                "address": body["address"],
                "register_inputs": sorted(inputs[name, version]),
                "register_outputs": [reg for reg in body["register_outputs"] if reg in ("r2", "f0")],
                "calls": calls_,
                "returns": [{"values": {reg: row["values"][reg] for reg in ("r2", "f0")}} for row in body["returns"]],
            }
        summaries[name] = {"versions": versions}
    facts = {"functions": summaries}
    calls: dict[str, list[dict[str, Any]]] = {}
    for item in facts["functions"].values():
        for body in item["versions"].values():
            for call in body["calls"]:
                if call["callee"]:
                    calls.setdefault(call["callee"], []).append(call)
    changed = True
    while changed:
        changed = False
        for name, item in facts["functions"].items():
            for version, body in item["versions"].items():
                used = inputs[name, version]
                for call in body["calls"]:
                    for reg in inputs.get((call["callee"], version), set()):
                        for origin in call["arguments"].get(reg, {}).get("origins", []):
                            prefix = f"param:{name}:"
                            if origin["id"].startswith(prefix):
                                actual = origin["id"][len(prefix) :]
                                if argument(actual) and actual not in used:
                                    used.add(actual)
                                    changed = True
    available: dict[str, set[str]] = {name: set() for name in facts["functions"]}

    def is_defined(name: str, version: str, reg: str, value: dict[str, Any]) -> bool:
        if value.get("unknown"):
            return False
        origins = value.get("origins", [])
        if origins == [{"id": f"param:{name}:{reg}", "offset": 0}]:
            return False
        for origin in origins:
            if origin["id"].startswith("return:"):
                _, caller, call_version, call_index, return_reg = origin["id"].split(":")
                body = facts["functions"][caller]["versions"][call_version]
                callee = next(
                    (
                        call["callee"]
                        for call in body["calls"]
                        if (call["instruction"] - body["address"]) // 4 == int(call_index)
                    ),
                    None,
                )
                if return_reg not in available.get(str(callee), set()):
                    return False
        return bool(origins) or value.get("constant") is not None

    for _ in range(len(available)):
        changed = False
        for name, item in facts["functions"].items():
            for version, body in item["versions"].items():
                for exit_ in body["returns"]:
                    for reg in ("r2", "f0"):
                        if reg not in available[name] and is_defined(name, version, reg, exit_["values"][reg]):
                            available[name].add(reg)
                            changed = True
                for call in body["calls"]:
                    if call.get("tail"):
                        old = set(available[name])
                        available[name].update(available.get(call["callee"], set()))
                        changed |= old != available[name]
        if not changed:
            break
    output = {}
    for name, item in facts["functions"].items():
        observed = [inputs[name, version] for version in item["versions"]]
        regs = set.union(*observed) if observed else set()
        conflicts = []
        if any(row != regs for row in observed):
            conflicts.append("callee input registers differ across versions")
        missing = []
        for call in calls.get(name, []):
            for reg in regs:
                if call["arguments"].get(reg, {}).get("unknown", True):
                    missing.append(
                        {
                            "function": call["function"],
                            "version": call["version"],
                            "instruction": call["instruction"],
                            "register": reg,
                        }
                    )
        return_regs = set()
        return_incomplete = False
        for body in item["versions"].values():
            defined = set()
            for returned in body["returns"]:
                for reg in ("r2", "f0"):
                    value = returned["values"][reg]
                    if is_defined(
                        name, next(v for v, candidate in item["versions"].items() if candidate is body), reg, value
                    ):
                        defined.add(reg)
                    elif reg in body["register_outputs"]:
                        return_incomplete = True
            return_regs.update(defined)
        used_returns = {
            reg
            for call in calls.get(name, [])
            for reg, uses in call.get("return_register_use", {}).items()
            if uses and reg in ("r2", "f0")
        }
        if used_returns - return_regs:
            conflicts.append("callers consume return registers not defined at callee exits")
        # GPR temporaries can coexist with an FP result. Caller consumption takes
        # precedence over incidental exit register contents.
        returned = used_returns or ({"f0"} if "f0" in return_regs else return_regs)
        if len(returned) > 1:
            conflicts.append("callers disagree on integer versus floating return ABI")
        if returned:
            selected = next(iter(returned))
            for body in item["versions"].values():
                for exit_ in body["returns"]:
                    value = exit_["values"][selected]
                    if value.get("unknown") or value.get("origins") == [
                        {"id": f"param:{name}:{selected}", "offset": 0}
                    ]:
                        return_incomplete = True
        output[name] = {
            "registers": sorted(regs),
            "return_register": next(iter(returned)) if len(returned) == 1 else None,
            "void": not returned and not return_incomplete,
            "return_known": not return_incomplete,
            "arity_known": not conflicts and not missing,
            "conflicts": conflicts,
            "missing": missing,
            "call_sites": len(calls.get(name, [])),
            "inputs": {version: sorted(inputs[name, version]) for version in item["versions"]},
        }
    # A tail call inherits the callee return ABI. Cycles with no evidenced exit
    # remain unresolved rather than becoming void by absence.
    for _ in range(len(output)):
        changed = False
        for name, item in facts["functions"].items():
            tails = {call["callee"] for body in item["versions"].values() for call in body["calls"] if call.get("tail")}
            if not tails:
                continue
            candidates = {
                (output[callee]["return_register"], output[callee]["void"], output[callee]["return_known"])
                for callee in tails
                if callee in output
            }
            if len(candidates) != 1:
                output[name]["return_known"] = False
                continue
            reg, void, known = next(iter(candidates))
            row = output[name]
            if (row["return_register"], row["void"], row["return_known"]) != (reg, void, known):
                row.update(return_register=reg, void=void, return_known=known)
                changed = True
        if not changed:
            break
    return output


def parameters(registers: list[str], types: dict[str, str | None]) -> list[str] | None:
    """Require every occupied O32 slot; a hole does not invent a parameter."""
    regs = set(registers)
    result = []
    slot = 0
    if "f12" in regs:
        if "r4" in regs:
            return None
        result.append("f12")
        slot += 2 if types.get("f12") == "double" else 1
        if "f14" in regs:
            result.append("f14")
            if types.get("f14") == "double":
                slot += slot % 2
                slot += 2
            else:
                slot += 1
    elif "f14" in regs:
        return None
    remaining = regs - set(result)
    while remaining:
        reg = f"r{4 + slot}" if slot < 4 else f"stack{slot * 4}"
        if reg not in remaining:
            # A 64-bit scalar aligns to an even slot; never fill unused holes.
            aligned = slot + slot % 2
            candidate = f"r{4 + aligned}" if aligned < 4 else f"stack{aligned * 4}"
            if types.get(candidate) not in ("double", "long long", "unsigned long long") or candidate not in remaining:
                return None
            slot, reg = aligned, candidate
        result.append(reg)
        remaining.remove(reg)
        slot += 2 if types.get(reg) in ("double", "long long", "unsigned long long") else 1
    return result
