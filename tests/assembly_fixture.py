"""Encode the deliberately small assembly notation used by ELF fixtures."""

import ast
import re
import struct

from tests.elf_fixture import write_object

REGISTERS = dict(zero=0, at=1, v0=2, v1=3, a0=4, a1=5, a2=6, a3=7, sp=29, ra=31)
REGISTERS.update({f"t{i}": 8 + i for i in range(8)})
REGISTERS.update({f"s{i}": 16 + i for i in range(8)})


def object_fixture(path, text):
    sections = {".text": bytearray()}
    section = ".text"
    labels, globals_, functions, sizes, relocations, fixups = {}, set(), set(), {}, [], []

    def register(value):
        value = value.strip().removeprefix("$")
        return int(value.removeprefix("f")) if value.removeprefix("f").isdigit() else REGISTERS[value]

    def word(value):
        sections[section].extend(struct.pack(">I", value & 0xFFFFFFFF))

    def reference(expression, kind, offset):
        match = re.fullmatch(r"([\w.$]+)(?:\s*([+-])\s*(0x[\da-fA-F]+|\d+))?", expression.strip())
        if not match:
            return int(expression, 0)
        name = match[1]
        try:
            return int(name, 0)
        except ValueError:
            addend = int(match[3], 0) * (-1 if match[2] == "-" else 1) if match[3] else 0
            relocations.append((section, offset, kind, name))
            return addend

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([\w.$]+):\s*(.*)$", line)
        if match:
            labels[match[1]] = (section, len(sections[section]))
            line = match[2]
            if not line:
                continue
        op, _, operand = line.partition(" ")
        operand = operand.strip()
        if op in (".text", ".data", ".rdata", ".rodata", ".bss") or op == ".section":
            section = operand.split(",")[0] if op == ".section" else op
            sections.setdefault(section, bytearray())
        elif op in (".globl", ".global"):
            globals_.update(operand.split(","))
        elif op == ".type":
            if "function" in operand:
                functions.add(operand.split(",")[0])
        elif op in (".equ", ".equiv", ".set") and "," in operand and operand.split(",", 1)[1].strip().startswith("0x"):
            name, value = operand.split(",", 1)
            labels[name] = ("ABS", int(value, 0))
        elif op == ".size":
            name, expression = operand.split(",", 1)
            sizes[name.strip()] = (
                len(sections[section]) - labels[name.strip()][1]
                if ".-" in expression.replace(" ", "")
                else int(expression, 0)
            )
        elif op in (".set", ".ent", ".end", ".option", ".abicalls"):
            pass
        elif op in (".align", ".balign", ".p2align"):
            align = int(operand.split(",")[0], 0)
            if op != ".balign":
                align = 1 << align
            sections[section].extend(bytes(-len(sections[section]) % align))
        elif op in (".space", ".zero"):
            sections[section].extend(bytes(int(operand, 0)))
        elif op in (".ascii", ".asciz", ".asciiz"):
            sections[section].extend(ast.literal_eval(operand).encode())
            if op != ".ascii":
                sections[section].append(0)
        elif op in (".byte", ".short", ".half", ".word", ".float", ".double"):
            for item in operand.split(","):
                if op in (".float", ".double"):
                    sections[section].extend(struct.pack(">f" if op == ".float" else ">d", float(item)))
                elif op == ".word":
                    word(reference(item, 2, len(sections[section])))
                else:
                    size = 1 if op == ".byte" else 2
                    sections[section].extend(int(item, 0).to_bytes(size, "big"))
        else:
            args = [a.strip() for a in operand.split(",")]
            at = len(sections[section])
            if op == "nop":
                value = 0
            elif op == "jr":
                value = register(args[0]) << 21 | 8
            elif op in ("j", "jal"):
                value = (2 if op == "j" else 3) << 26 | (reference(args[0], 4, at) >> 2 & 0x3FFFFFF)
            elif op in ("beq", "bne"):
                fixups.append((section, at, args[2]))
                value = (4 if op == "beq" else 5) << 26 | register(args[0]) << 21 | register(args[1]) << 16
            elif op == "lui":
                expression = args[1]
                immediate = (
                    (reference(expression[4:-1], 5, at) + 0x8000) >> 16
                    if expression.startswith("%hi(")
                    else int(expression, 0)
                )
                value = 0x3C000000 | register(args[0]) << 16 | immediate & 0xFFFF
            elif op in ("lw", "lwc1", "sw", "swc1", "lb", "lbu"):
                match = re.fullmatch(r"(.+)\((\$\w+)\)", args[1])
                assert match, args
                immediate = reference(match[1][4:-1], 6, at) if match[1].startswith("%lo(") else int(match[1], 0)
                value = (
                    {
                        "lw": 0x8C000000,
                        "lwc1": 0xC4000000,
                        "sw": 0xAC000000,
                        "swc1": 0xE4000000,
                        "lb": 0x80000000,
                        "lbu": 0x90000000,
                    }[op]
                    | register(match[2]) << 21
                    | register(args[0]) << 16
                    | immediate & 0xFFFF
                )
            elif op in ("addiu", "ori", "andi"):
                immediate = reference(args[2][4:-1], 6, at) if args[2].startswith("%lo(") else int(args[2], 0)
                value = (
                    {"addiu": 0x24000000, "ori": 0x34000000, "andi": 0x30000000}[op]
                    | register(args[1]) << 21
                    | register(args[0]) << 16
                    | immediate & 0xFFFF
                )
            elif op == "addu":
                value = register(args[1]) << 21 | register(args[2]) << 16 | register(args[0]) << 11 | 0x21
            else:
                raise AssertionError(f"fixture notation needs explicit encoding for {line}")
            word(value)
    for sec, at, name in fixups:
        label, _, delta = name.partition("+")
        immediate = (labels[label][1] + (int(delta, 0) if delta else 0) - at - 4) // 4
        value = struct.unpack_from(">I", sections[sec], at)[0] | (immediate & 0xFFFF)
        struct.pack_into(">I", sections[sec], at, value)
    for name in sections:
        labels.setdefault(name, (name, 0))
    # REL addends for local symbols retain their section offsets.
    for section, at, kind, name in relocations:
        if name in labels and name not in globals_ and not (kind == 4 and not name.startswith(".L")):
            _target_section, offset = labels[name]
            value = struct.unpack_from(">I", sections[section], at)[0]
            value += (
                offset if kind == 2 else offset >> 2 if kind == 4 else (offset + 0x8000) >> 16 if kind == 5 else offset
            )
            struct.pack_into(">I", sections[section], at, value & 0xFFFFFFFF)
    relocations = [
        (
            sec,
            at,
            kind,
            labels[name][0]
            if name in labels and name not in globals_ and not (kind == 4 and not name.startswith(".L"))
            else name,
        )
        for sec, at, kind, name in relocations
    ]
    symbols = [
        (
            name,
            sec,
            offset,
            sizes.get(name, len(sections[sec]) - offset if name in functions else 0),
            0x12
            if name in functions or (name in globals_ and sec == ".text")
            else 0x10
            if name in globals_
            else 3
            if name in sections
            else 0,
        )
        for name, (sec, offset) in labels.items()
    ]
    symbols.extend((name, None, 0, 0, 0x10) for name in dict.fromkeys(r[3] for r in relocations) if name not in labels)
    return write_object(path, {key: bytes(value) for key, value in sections.items()}, symbols, relocations=relocations)
