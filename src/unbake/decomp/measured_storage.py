"""Materialize measured stack intervals and opaque byte-address transports."""

import re
from pathlib import Path

from unbake.decomp.draft_macros import calls
from unbake.layout.structs_types import SCALARS
from unbake.config import Held, Project
from unbake.project_tools import atomic as atomic_files


def prepare(project: Project, function: str, source: str, assembly: str) -> tuple[str, Path | None]:
    """Use instruction widths and stack annotations without settling unknown types."""
    shared = None
    template = re.search(
        r"struct _m2c_stack_" + re.escape(function) + r"\s*\{(.*?)\};\s*/\* size = (0x[\da-fA-F]+) \*/", source, re.S
    )
    if template and "M2C_UNK" in template[1]:
        rows = list(re.finditer(r"/\*\s*(0x[\da-fA-F]+)\s*\*/\s*([^;]+);", template[1]))
        size = int(template[2], 16)
        fields = {}
        for row in rows:
            offset = int(row[1], 16)
            declaration = row[2].strip()
            match = re.fullmatch(r"(.+?)\s+(sp[\da-fA-F]+)(?:\[(\d+|0x[\da-fA-F]+)\])?", declaration)
            if match is None:
                continue
            type_, name, extent = match.groups()
            if type_ == "M2C_UNK":
                word = re.search(r"\b" + name + r"\s*=\s*M2C_UNALIGNED32\(", source)
                if word:
                    type_ = "unsigned int"
                else:
                    following = min((int(r[1], 16) for r in rows if int(r[1], 16) > offset), default=size)
                    # A padding annotation at the same address bounds opaque
                    # address-taken storage, never an inferred scalar.
                    pad = next(
                        (
                            re.search(r"\bpad\w*\[(0x[\da-fA-F]+|\d+)\]", r[2])
                            for r in rows
                            if int(r[1], 16) == offset and "pad" in r[2]
                        ),
                        None,
                    )
                    if pad is None or not re.search(r"&" + name + r"\b", source):
                        raise Held("m2c", f"{function}.{name}: unknown stack storage lacks a measured extent")
                    extent = str(min(int(pad[1], 0), following - offset))
                    type_ = "unsigned char"
            width = SCALARS.get(type_, (None, None))[0]
            if width is None or offset + width * (int(extent, 0) if extent else 1) > size:
                raise Held("m2c", f"{function}.{name}: unsupported measured stack interval")
            fields[name] = (offset, f"{type_} {name}" + (f"[{extent}]" if extent else "") + ";")
            if width != 4 and re.search(r"\b" + name + r"\s*=\s*M2C_UNALIGNED32\(", source):
                word_name = "word_" + name
                fields[word_name] = (offset, f"unsigned int {word_name};")
                source = re.sub(r"\b" + name + r"(?=\s*=\s*M2C_UNALIGNED32\()", word_name, source)
        tag = "MeasuredStack_" + function
        lines = [f"struct {tag} {{ union {{", f"unsigned char bytes[{size}];"]
        for name, (offset, declaration) in fields.items():
            padding = f"unsigned char padding[{offset}];" if offset else ""
            lines.append(f"struct {{ {padding} {declaration} }} slot_{name};")
        lines.append("} storage; };")
        shared = project.include[0] / "common" / f"draft_stack_{function}.h"
        guard = "UNBAKE_DRAFT_STACK_" + function.upper() + "_H"
        atomic_files.text(shared, f"#ifndef {guard}\n#define {guard}\n" + "\n".join(lines) + "\n#endif\n")
        source = source[: template.start()] + source[template.end() :]
        for name in fields:
            source = re.sub(
                r"(?m)^\s*(?:M2C_UNK|[su]\d+|unsigned int|unsigned char)\s+" + name + r"(?:\[[^]]*\])?;\s*\n",
                "\n",
                source,
            )
        token = re.compile(r"/\*.*?\*/|//[^\n]*|\b[A-Za-z_]\w*\b", re.S)
        source = token.sub(lambda m: f"frame.storage.slot_{m[0]}.{m[0]}" if m[0] in fields else m[0], source)
        entry = re.search(r"\b" + re.escape(function) + r"\s*\([^{};]*\)\s*\{", source)
        if entry is None:
            raise Held("m2c", f"{function}: missing measured stack body")
        source = source[: entry.end()] + f"\n    struct {tag} frame;" + source[entry.end() :]
        # Unknown pointer arithmetic in m2c is byte arithmetic. The stack
        # interval proves storage, but does not establish an aggregate type.
        source = re.sub(r"\bM2C_UNK\s*\*\s*(\w+)\s*;", r"unsigned char *\1;", source)
        source = re.sub(r"(\b\w+\s*=\s*)&(frame\.storage\.slot_\w+\.\w+);", r"\1(unsigned char *)&\2;", source)

    # Unknown data names are address transports only. Read widths below come
    # from machine accesses, independently of their semantic target layout.
    opaque = set(re.findall(r"extern M2C_UNK (\w+);", source))
    for name in sorted(opaque):
        widths = set()
        for match in re.finditer(r"\b(lh|lhu|lw|lb|lbu)\s+[^\n]*%lo\(" + re.escape(name) + r"\)", assembly):
            widths.add(
                {"lh": "short", "lhu": "unsigned short", "lw": "int", "lb": "signed char", "lbu": "unsigned char"}[
                    match[1]
                ]
            )
        load = re.compile(r"\*\(&" + re.escape(name) + r"\s*\+\s*(\w+)\)")
        if re.search(r"\bjalr\b", assembly) and re.search(load.pattern + r"(?=\s*\()", source):

            def function_word(match: re.Match[str], symbol: str = name) -> str:
                return f"((void (**)())(&{symbol} + {match[1]}))[0]"

            def address_word(match: re.Match[str], symbol: str = name) -> str:
                return f"*((unsigned int *)(&{symbol} + {match[1]}))"

            source = re.sub(load.pattern + r"(?=\s*\()", function_word, source)
            source = load.sub(address_word, source)
        elif len(widths) == 1:
            type_ = next(iter(widths))

            def scalar_load(match: re.Match[str], symbol: str = name, spelling: str = type_) -> str:
                return f"*(({spelling} *)(&{symbol} + {match[1]}))"

            source = load.sub(scalar_load, source)
        elif load.search(source):
            unaligned_load = re.compile(r"M2C_UNALIGNED32\(\s*" + load.pattern + r"\s*\)")
            remaining = unaligned_load.sub("", source)
            if load.search(remaining):
                raise Held("m2c", f"{function}.{name}: data load lacks a measured width")
        uses = re.sub(r"extern M2C_UNK " + re.escape(name) + r";", "", source)
        if any(
            not uses[: m.start()].rstrip().endswith("&") for m in re.finditer(r"\b" + re.escape(name) + r"\b", uses)
        ):
            continue
        source = source.replace(
            f"extern M2C_UNK {name};", f"extern unsigned char {name}; /* opaque address transport */"
        )
    addresses: list[str] = []

    def unaligned(args: list[str]) -> str:
        if len(args) != 1:
            raise Held("m2c", f"{function}: invalid unaligned word operand")
        name = "unbake_bytes_" + str(len(addresses))
        while re.search(r"\b" + name + r"\b", source):
            name += "_"
        addresses.append(name)
        return (
            f"({name} = (const unsigned char *)&({args[0]}), "
            f"((unsigned int){name}[0] << 24) | ((unsigned int){name}[1] << 16) | "
            f"((unsigned int){name}[2] << 8) | {name}[3])"
        )

    source = calls(source, "M2C_UNALIGNED32", unaligned)
    if addresses:
        entry = re.search(r"\b" + re.escape(function) + r"\s*\([^{};]*\)\s*\{", source)
        if entry is None:
            raise Held("m2c", f"{function}: missing unaligned word body")
        declarations = "".join(f"\n    const unsigned char *{name};" for name in addresses)
        source = source[: entry.end()] + declarations + source[entry.end() :]
    return source, shared
