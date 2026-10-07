"""Materialize measured stack intervals and opaque byte-address transports."""

import re
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.cdecl import SOURCE_TOKEN, declaration_source
from unbake.config import Held, Project
from unbake.decomp import measured_access
from unbake.decomp.draft_macros import calls
from unbake.layout.structs_types import SCALARS


def _access_views(function: str, source: str, assembly: str, symbol: str, reads: set[str], writes: set[str]) -> str:
    """Measure reads and lvalues independently; never infer a shared pointee type.

    Instruction order is not C expression order. Mixed signed loads can only be
    lowered when an explicit narrow conversion makes their value unambiguous.
    A same-width store transports the bits without extending the loaded value.
    """
    clean = declaration_source(source)
    tokens = [m for m in SOURCE_TOKEN.finditer(clean) if not m[0].startswith(('"', "'"))]
    declarations = {
        m[2]: m[1]
        for m in re.finditer(r"\b(s8|u8|s16|u16|signed char|unsigned char|short|unsigned short)\s+(\w+)\s*;", clean)
    }
    aliases = {"s8": "signed char", "u8": "unsigned char", "s16": "short", "u16": "unsigned short"}
    edits: list[tuple[int, int, str, int]] = []
    for index, token in enumerate(tokens):
        if [m[0] for m in tokens[index : index + 5]] != ["*", "(", "&", symbol, "+"]:
            continue
        level = 1
        tail = index + 5
        while tail < len(tokens):
            level += (tokens[tail][0] == "(") - (tokens[tail][0] == ")")
            if not level:
                break
            tail += 1
        if level:
            raise Held("m2c", f"{function}.{symbol}: unclosed data access")
        end = tokens[tail].end()
        after = clean[end:].lstrip()
        before = clean[: token.start()]
        assignment = re.match(r"=(?!=)", after)
        compound = re.match(r"(?:[+*/%&|^-]|<<|>>)=|\+\+|--", after) or re.search(r"(?:\+\+|--)\s*$", before)
        unaligned = re.search(r"\bM2C_UNALIGNED32\(\s*$", before)
        if unaligned and not assignment and not compound:
            continue
        address = source[tokens[index + 5].start() : tokens[tail].start()].strip()
        byte_address = f"((unsigned char *)&{symbol} + {address})"
        if after.startswith("(") and re.search(r"\bjalr\b", assembly):
            if reads != {"int"}:
                raise Held("m2c", f"{function}.{symbol}: indirect call lacks a measured word")
            value = f"((void (**)()){byte_address})[0]"
        else:
            choices = writes if assignment else reads
            widths = {SCALARS[spelling][0] for spelling in choices}
            if len(widths) != 1:
                kind = "store" if assignment else "load"
                raise Held("m2c", f"{function}.{symbol}: data {kind} lacks a measured width")
            width = next(iter(widths))
            if compound and {SCALARS[spelling][0] for spelling in writes} != {width}:
                raise Held("m2c", f"{function}.{symbol}: read/modify/write lacks a measured width")
            if len(choices) == 1:
                spelling = next(iter(choices))
            else:
                # A narrow assignment or cast occurs before promotion. Both
                # machine extensions produce the same bits after that conversion.
                cast = re.search(r"\((s8|u8|s16|u16|signed char|unsigned char|short|unsigned short)\)\s*$", before)
                destination = re.search(r"\b(\w+)\s*=\s*$", before)
                spelling = aliases.get(cast[1], cast[1]) if cast else ""
                if not spelling and destination and re.match(r"\s*;", after):
                    spelling = declarations.get(destination[1], "")
                    spelling = aliases.get(spelling, spelling)
                # The RHS of a measured same-width store only transports bits.
                transport = any(
                    stop <= token.start()
                    and re.fullmatch(r"\s*=\s*", clean[stop : token.start()])
                    and view_width == width
                    and re.match(r"\s*;", after)
                    for _, stop, _, view_width in edits
                )
                if transport:
                    spelling = {1: "unsigned char", 2: "unsigned short", 4: "int"}[width]
                if compound or not spelling or SCALARS[spelling][0] != width:
                    raise Held("m2c", f"{function}.{symbol}: data load lacks a measured signed view")
            value = f"*(({spelling} *){byte_address})"
        edits.append((token.start(), end, value, 4 if after.startswith("(") else width))
    for start, end, value, _ in reversed(edits):
        source = source[:start] + value + source[end:]
    return source


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
        entry = re.search(r"\b" + re.escape(function) + r"\s*\([^{};]*\)\s*\{", source)
        if entry is None:
            raise Held("m2c", f"{function}: missing measured stack body")
        # The template measures the local SP origin. Use that same byte
        # storage for dynamic addresses, leaving their index and field offset
        # intact so static and dynamic accesses continue to alias.
        body = SOURCE_TOKEN.sub(
            lambda m: (
                f"frame.storage.slot_{m[0]}.{m[0]}"
                if m[0] in fields
                else "frame.storage.bytes"
                if m[0] == "sp"
                else m[0]
            ),
            source[entry.end() :],
        )
        source = source[: entry.end()] + f"\n    struct {tag} frame;" + body
        # Unknown pointer arithmetic in m2c is byte arithmetic. The stack
        # interval proves storage, but does not establish an aggregate type.
        source = re.sub(r"\bM2C_UNK\s*\*\s*(\w+)\s*;", r"unsigned char *\1;", source)
        source = re.sub(r"(\b\w+\s*=\s*)&(frame\.storage\.slot_\w+\.\w+);", r"\1(unsigned char *)&\2;", source)

    # Unknown data names are address transports only. Access widths below come
    # from machine accesses, independently of their semantic target layout.
    opaque = set(re.findall(r"extern M2C_UNK (\w+);", source))
    accesses = measured_access.views(assembly) if opaque else {}
    for name in sorted(opaque):
        reads, writes = accesses.get(name, (set(), set()))
        source = _access_views(function, source, assembly, name, reads, writes)
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
        operand = args[0]
        fields: list[list[str]] = []

        def note(field: list[str]) -> str:
            fields.append(field)
            return f"M2C_FIELD({', '.join(field)})"

        canonical = calls(operand, "M2C_FIELD", note)
        # An unaligned word consumes four bytes at the operand's address.
        # Its unknown scalar spelling establishes no pointee layout. Only
        # the outer lvalue supplies that address; nested bases still need
        # their own evidence before they can be lowered.
        if fields and canonical == f"M2C_FIELD({', '.join(fields[-1])})":
            field = fields[-1]
            if len(field) == 3 and re.fullmatch(r"M2C_UNK\d*\s*\*", field[1]):
                operand = f"M2C_FIELD({field[0]}, unsigned char *, {field[2]})"
        name = "unbake_bytes_" + str(len(addresses))
        while re.search(r"\b" + name + r"\b", source):
            name += "_"
        addresses.append(name)
        return (
            f"({name} = (const unsigned char *)&({operand}), "
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
