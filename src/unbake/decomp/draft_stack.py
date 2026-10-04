"""Preserve measured stack offsets when m2c emits arrays or raw SP accesses."""

import re

from unbake.config import Held
from unbake.layout.structs import Layout


def overlay(output: str, template: Layout, prefix_size: int, function: str, assembly: str) -> str | None:
    start, end = template.start - prefix_size, template.end - prefix_size
    text = output[start:end]
    rows = list(re.finditer(r"/\*\s*(0x[\dA-Fa-f]+)\s*\*/\s*([^;\n]+);", text))
    entry = re.search(rf"\b{re.escape(function)}\s*\([^;{{}}]*\)\s*{{", output[end:])
    if entry is None:
        raise Held("m2c", f"{function}: missing body for inferred stack declarations")
    entry_end = end + entry.end()
    body = output[entry_end:]
    if not (re.search(r"\b(?:sp|unksp[\dA-Fa-f]+)\b", body) or re.search(r"\[\s*\]", text)):
        return None
    if not rows:
        raise Held("m2c", f"{function}: raw stack access has no measured stack offsets")
    if len(rows) != len(template.fields):
        raise Held("m2c", f"{function}: stack offset annotations do not correspond to inferred declarations")
    offsets = [int(row[1], 16) for row in rows]
    members = {member.name: member for member in template.fields}
    size_match = re.match(r"\s*;\s*/\*\s*size\s*=?\s*(0x[\dA-Fa-f]+)", output[end:])
    frame_size = (
        int(size_match[1], 16)
        if size_match
        else max(offset + member.size for offset, member in zip(offsets, template.fields, strict=True))
    )
    views: dict[str, tuple[int, str]] = {}
    for row, offset in zip(rows, offsets, strict=True):
        declaration = row[2].strip() + ";"
        name_match = re.search(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*;", declaration)
        if name_match is None:
            raise Held("m2c", f"{function}: unsupported stack declaration {declaration}")
        name = name_match[1]
        member = members[name]
        if name.startswith("pad"):
            continue
        if re.search(r"\[\s*\]", declaration):
            following = min((value for value in offsets if value > offset), default=frame_size)
            scalar = member.type.split("[", 1)[0]
            from unbake.layout.structs_types import SCALARS

            if scalar not in SCALARS:
                raise Held("m2c", f"{function}.{name}: missing array element layout {scalar}")
            width = SCALARS[scalar][0]
            if following <= offset or (following - offset) % width:
                raise Held("m2c", f"{function}.{name}: cannot prove stack array extent")
            declaration = re.sub(r"\[\s*\]", f"[{(following - offset) // width}]", declaration, count=1)
        views[name] = (offset, declaration)
    for name in set(re.findall(r"\bunksp([\dA-Fa-f]+)\b", body)):
        offset = int(name, 16)
        # These are omitted GP stack reads, not invented untyped locals.
        load = re.search(rf"\blw\s+\$\w+,\s*0x0*{name}\(\$sp\)", assembly, re.I)
        if load is None:
            raise Held("m2c", f"{function}.unksp{name}: no word-load stack evidence")
        if offset + 4 > frame_size:
            raise Held(
                "m2c",
                f"{function}.unksp{name}: word read lies outside measured local frame 0x{frame_size:X}; "
                "incoming stack argument layout is unresolved",
            )
        views["unksp" + name] = (offset, f"s32 unksp{name};")
    locals_end = body.find("\n\n")
    locals_text = body[:locals_end] if locals_end >= 0 else ""
    for name in views:
        locals_text = re.sub(
            rf"^[ \t]*[^;\n{{}}]+\b{re.escape(name)}\s*(?:\[[^\]]*\]\s*)*;[^\n]*\n?",
            "",
            locals_text,
            flags=re.M,
        )
    body = locals_text + body[locals_end:] if locals_end >= 0 else body
    # The local union retains aliases and byte arithmetic through one object.
    lines = [f"\n    union {{ unsigned char bytes[0x{frame_size:X}];"]
    for name, (offset, declaration) in views.items():
        pad = f"unsigned char pad[0x{offset:X}]; " if offset else ""
        lines.append(f"        struct {{ {pad}{declaration} }} slot_{name};")
    lines.append("    } m2c_stack;\n")
    token = re.compile(r"/\*.*?\*/|//[^\n]*|\b[A-Za-z_]\w*\b", re.S)
    body = token.sub(
        lambda match: (
            f"m2c_stack.slot_{match[0]}.{match[0]}"
            if match[0] in views
            else "m2c_stack.bytes"
            if match[0] == "sp"
            else match[0]
        ),
        body,
    )
    # Remove the complete template statement, preserving comments and externs.
    trailing = re.match(r"\s*;", output[end:])
    template_end = end + trailing.end() if trailing else end
    return output[:start] + output[template_end:entry_end] + "\n".join(lines) + body
