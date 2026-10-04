"""Small objdiff JSON responses at the scoring subprocess boundary."""

import json
import struct
import subprocess
from pathlib import Path

from unbake.objects.elf import Object


def side(path, function):
    obj = Object(path)
    symbols = [{"name": "", "kind": "SYMBOL_UNKNOWN"}]
    definitions = [symbol for table in obj.symbols.values() for symbol in table if symbol["name"] == function]
    entry = definitions[0] if definitions else {"value": 0, "size": 0}
    text = obj.section(".text")
    content = obj.content(text) if text is not None else b""
    offset, size = entry["value"], entry["size"] or len(content)
    rows = []
    symbol = dict(
        name=function, kind="SYMBOL_FUNCTION", address=offset, size=size, match_percent=100.0, instructions=rows
    )
    symbols.append(symbol)
    rels = {at: (kind, target) for at, kind, target in obj.relocations(text)} if text else {}
    for at in range(offset, min(offset + size, len(content)), 4):
        word = struct.unpack_from(">I", content, at)[0]
        op = word >> 26
        parts = [{"opcode": str(op if op else word & 63)}]
        regs = [word >> 21 & 31, word >> 16 & 31] if op not in (2, 3, 15) else [word >> 16 & 31] if op == 15 else []
        if op == 0:
            regs = [word >> 21 & 31, word >> 16 & 31, word >> 11 & 31]
        parts.extend(
            {
                "arg": {
                    "opaque": [
                        "$zero",
                        "$at",
                        "$v0",
                        "$v1",
                        "$a0",
                        "$a1",
                        "$a2",
                        "$a3",
                        "$t0",
                        "$t1",
                        "$t2",
                        "$t3",
                        "$t4",
                        "$t5",
                        "$t6",
                        "$t7",
                        "$s0",
                        "$s1",
                        "$s2",
                        "$s3",
                        "$s4",
                        "$s5",
                        "$s6",
                        "$s7",
                        "$t8",
                        "$t9",
                        "$k0",
                        "$k1",
                        "$gp",
                        "$sp",
                        "$fp",
                        "$ra",
                    ][reg]
                }
            }
            for reg in regs
        )
        ins = dict(address=at, size=4, parts=parts, original=word)
        if at in rels:
            kind, target = rels[at]
            name = target["name"]
            section = target["section"]
            local = target["info"] & 15 == 3 and section
            if local:
                name = "[" + obj.names[section] + "]"
            addend = word & (0x3FFFFFF if kind == 4 else 0xFFFF)
            if kind == 4:
                addend <<= 2
            elif kind == 5:
                following = next(
                    (
                        i
                        for i in range(at + 4, len(content), 4)
                        if i in rels and rels[i][0] == 6 and rels[i][1]["name"] == target["name"]
                    ),
                    None,
                )
                low = struct.unpack_from(">I", content, following)[0] & 0xFFFF if following is not None else 0
                addend = (addend << 16) + (low - 65536 if low & 0x8000 else low)
            elif addend & 0x8000:
                addend -= 0x10000
            target_index = len(symbols)
            symbols.append(dict(name=name, kind="SYMBOL_UNKNOWN", address=target["value"], size=target["size"]))
            ins["formatted"] = name
            ins["relocation"] = dict(type=kind, target_symbol=target_index, addend=addend)
            if kind == 4 and section and obj.names[section] == ".text":
                ins["branch_dest"] = target["value"] + addend
            parts.append({"arg": {"reloc": True}})
        else:
            parts.append({"arg": {"signed": word & 0xFFFF}})
        rows.append(dict(instruction=ins, diff_kind="DIFF_NONE"))
    return {"symbols": symbols}


def output(command, **kwargs):
    assert command[1] == "diff", command
    function = command[command.index("-2") + 2]
    left = side(Path(command[command.index("-1") + 1]), function)
    right = side(Path(command[command.index("-2") + 1]), function)
    a, b = left["symbols"][1]["instructions"], right["symbols"][1]["instructions"]
    equal = 0
    for index in range(max(len(a), len(b))):
        x, y = a[index] if index < len(a) else None, b[index] if index < len(b) else None
        same = x is not None and y is not None and x["instruction"]["parts"] == y["instruction"]["parts"]
        if same:
            u, v = x["instruction"].get("relocation"), y["instruction"].get("relocation")
            if u and v:
                same = (left["symbols"][u["target_symbol"]]["name"], u["addend"]) == (
                    right["symbols"][v["target_symbol"]]["name"],
                    v["addend"],
                )
        equal += same
        kind = "DIFF_NONE" if same else "DIFF_ARG_MISMATCH"
        if x and y and x["instruction"]["parts"][0] != y["instruction"]["parts"][0]:
            kind = "DIFF_REPLACE"
        for row in (x, y):
            if row:
                row["diff_kind"] = kind
    left["symbols"][1]["match_percent"] = equal / len(a) * 100 if a else 0
    Path(command[command.index("--output") + 1]).write_text(json.dumps(dict(left=left, right=right)))
    return subprocess.CompletedProcess(command, 0, "", "")


def install(case):
    from tests.process_fakes import boundary
    from unbake.decomp import score

    mock = boundary(score, output)
    mock.start()
    case.addCleanup(mock.stop)
