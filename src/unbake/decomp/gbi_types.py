"""Canonical packet storage and scalar declarations for command recovery."""

from __future__ import annotations

import re

from unbake.decomp import gbi_audio
from unbake.decomp.gbi_source import gfx_typedefs, packet_pointers, tokens, typedefs
from unbake.config import Held, Project


def scalar_signature(declaration: str) -> list[str]:
    declaration = re.sub(r"\bsigned\s+(?=int|short|long)", "", declaration)
    declaration = re.sub(r"\b(short|long long|long)\s+int\b", r"\1", declaration)
    return tokens(declaration)


def packet_shape(declaration: str, words_type: str | None = None) -> bool:
    """Require the complete two-word layout, rather than matching two field names."""
    declaration = " ".join(tokens(declaration))
    aggregate = re.fullmatch(r"typedef (struct|union)(?: \w+)? \{ (.*) \} \w+ ;", declaration)
    if not aggregate:
        return False
    body = aggregate[2]
    word = r"(?:u32|unsigned int) w0 ; (?:u32|unsigned int) w1 ;"
    if re.fullmatch(word, body):
        return aggregate[1] == "struct"
    storage = (re.escape(words_type) + r" words ;") if words_type else (r"struct \{ " + word + r" \} words ;")
    alignment = r"(?:s64|u64|long long(?: int)?|unsigned long long(?: int)?|double|f64) \w+ ;"
    return bool(re.fullmatch(storage + (r"(?: " + alignment + r")?" if aggregate[1] == "union" else ""), body))


def reuse_shared(project: Project, source: str) -> str:
    """Reuse identical named aggregates already present in project shared headers."""
    local_types = typedefs(source)
    edits = []
    includes = set()
    for name, (start, end, declaration) in local_types.items():
        shape = re.search(r"\b(struct|union)\s*(?:\w+\s*)?\{([^{}]+)\}", declaration)
        if not shape:
            continue
        destination = next(
            (
                path
                for root in project.include
                for path in sorted(root.glob("*.h"))
                if (entry := typedefs(path.read_text()).get(name))
                and (existing := re.search(r"\b(struct|union)\s*(?:\w+\s*)?\{([^{}]+)\}", entry[2]))
                and existing[1] == shape[1]
                and tokens(existing[2]) == tokens(shape[2])
            ),
            None,
        )
        if destination:
            edits.append((start, end))
            includes.add(f'#include "{destination.name}"')
    for start, end in sorted(edits, reverse=True):
        source = source[:start] + source[end:]
    for include in sorted(includes, reverse=True):
        if include not in source:
            source = include + "\n" + source
    return source


def audio_callbacks(source: str) -> str:
    """Remove exact callback typedef copies along with their Acmd storage copies."""
    pattern = re.compile(r"\btypedef\s+[^;{}]+?\(\s*\*\s*(\w+)\s*\)[^;{}]+;", re.S)
    expected = {
        match[1]: match[0] for match in pattern.finditer(gbi_audio.HEADER.with_name("audio_callbacks.h").read_text())
    }

    def normalized(text: str) -> list[str]:
        text = re.sub(r"\bs16\b", "short", text)
        text = re.sub(r"\bs32\b", "int", text)
        return scalar_signature(text)

    changed = False
    for match in reversed(list(pattern.finditer(source))):
        if match[1] in expected and normalized(match[0]) == normalized(expected[match[1]]):
            source = source[: match.start()] + source[match.end() :]
            changed = True
    include = '#include "audio_callbacks.h"'
    if changed and include not in source:
        source = include + "\n" + source
    return source


def sdk_views(project: Project, source: str) -> str:
    """Recognize flat color vertices and a float matrix that are SDK name collisions."""
    declarations = typedefs(source)
    vertex = declarations.get("Vtx")
    if vertex:
        body = re.search(r"\{([^{}]+)\}", vertex[2])
        expected = "s16 x; s16 y; s16 z; s16 flag; s16 s; s16 t; u8 r; u8 g; u8 b; u8 a;"
        if body and tokens(body[1]) == tokens(expected):
            names = packet_pointers(source, "Vtx")
            # The local signed flag differs from the SDK unsigned flag. Preserve
            # this declaration if the field participates in any expression.
            if any(re.search(r"\b" + name + r"(?:\s*\[[^]]+\])?\s*(?:->|\.)\s*flag\b", source) for name in names):
                raise Held("gbi", "flat Vtx uses a signed flag; SDK view would change its type")
            source = source[: vertex[0]] + source[vertex[1] :]
            fields = {
                "x": "v.ob[0]",
                "y": "v.ob[1]",
                "z": "v.ob[2]",
                "s": "v.tc[0]",
                "t": "v.tc[1]",
                "r": "v.cn[0]",
                "g": "v.cn[1]",
                "b": "v.cn[2]",
                "a": "v.cn[3]",
            }
            for name in names:
                pattern = r"\b" + name + r"(?:\s*\[[^]]+\])?\s*(?:->|\.)\s*(x|y|z|s|t|r|g|b|a)\b"
                source = re.sub(pattern, lambda m: m[0][: m.start(1) - m.start()] + fields[m[1]], source)
    declarations = typedefs(source)
    matrix = declarations.get("Mtx")
    if matrix:
        body = re.search(r"\{([^{}]+)\}", matrix[2])
        if body and tokens(body[1]) == tokens("f32 m[4][4];"):
            match = next(
                (
                    (path, found[1])
                    for root in project.include
                    for path in sorted(root.glob("*.h"))
                    if (found := re.search(r"\btypedef\s+f32\s+(\w+)\s*\[4\]\s*\[4\]\s*;", path.read_text()))
                ),
                None,
            )
            if match is None:
                raise Held("gbi", "float Mtx view has no existing shared matrix declaration")
            if re.search(r"(?:->|\.)\s*m\b", source):
                raise Held("gbi", "float Mtx member access needs a proven array view")
            path, name = match
            source = source[: matrix[0]] + source[matrix[1] :]
            source = re.sub(r"\bMtx\b", name, source)
            source = f'#include "{path.name}"\n' + source
            # Reuse the vector declarations brought in by the matrix header's
            # dependencies, instead of introducing another local type collision.
            for local, (start, end, declaration) in sorted(
                typedefs(source).items(), key=lambda item: item[1][0], reverse=True
            ):
                aggregate = re.search(r"\{([^{}]+)\}", declaration)
                if not aggregate:
                    continue
                existing = next(
                    (
                        header
                        for root in project.include
                        for header in sorted(root.glob("*.h"))
                        if (entry := typedefs(header.read_text()).get(local))
                        and (shape := re.search(r"\{([^{}]+)\}", entry[2]))
                        and tokens(shape[1]) == tokens(aggregate[1])
                    ),
                    None,
                )
                if existing:
                    source = source[:start] + source[end:]
                    include = f'#include "{existing.name}"\n'
                    if include.strip() not in source:
                        source = include + source
    return source


def canonical(project: Project, source: str) -> str:
    if '#include "acmd.h"' in source or "Acmd" in typedefs(source):
        source = audio_callbacks(source)
    if gfx_typedefs(source):
        source = sdk_views(project, source)
    sdk = next((root / "n64sdk.h" for root in project.include if (root / "n64sdk.h").is_file()), None)
    source_types = typedefs(source)
    gfx_spans = gfx_typedefs(source)
    gfx_aliases = re.findall(r"\btypedef\s+(\w+)\s+Gfx\s*;", source)
    audio = source_types.get("Acmd")
    if not gfx_spans and not gfx_aliases and not audio:
        return reuse_shared(project, source) if '#include "n64sdk.h"' in source else source
    scalar = next(
        (
            path
            for root in project.include
            for path in sorted(root.glob("*.h"))
            if all(name in typedefs(path.read_text()) for name in ("u8", "u16", "u32", "s32", "s64", "f32"))
        ),
        None,
    )
    if (gfx_spans or gfx_aliases) and (sdk is None or scalar is None):
        return source
    removals: list[tuple[int, int]] = []
    includes: list[str] = []
    direct = False
    flattened = False
    gfx_pointers = packet_pointers(source, "Gfx")
    if gfx_spans or gfx_aliases:
        assert sdk is not None and scalar is not None
        for start, end in gfx_spans:
            declaration = source[start:end]
            if not packet_shape(declaration):
                raise Held("gbi", "local Gfx storage is not a proven two-word packet")
            direct |= not bool(re.search(r"\bwords\b", declaration))
            removals.append((start, end))
        for target in gfx_aliases:
            if target == "Gfx":
                continue
            # Follow included type declarations instead of guessing from a name.
            context = "\n".join(
                path.read_text()
                for root in project.include
                for path in sorted(root.rglob("*.h"))
                if path.is_file() and not path.is_symlink()
            )
            shape = re.search(r"\bstruct\s+" + re.escape(target) + r"\s*\{([^{}]+)\}\s*;", context)
            if shape is None or tokens(shape[1]) != ["u32", "words_w0", ";", "u32", "words_w1", ";"]:
                raise Held("gbi", f"Gfx alias {target}: storage layout is not proven SDK-compatible")
            flattened = True
            removals.append(source_types["Gfx"][:2])
        # Remove identical scalar declarations before inserting their shared header.
        scalar_types = typedefs(scalar.read_text())
        sdk_types = typedefs(sdk.read_text())
        for name, (start, end, declaration) in source_types.items():
            if name == "Gfx":
                continue
            if name in scalar_types:
                if scalar_signature(declaration) != scalar_signature(scalar_types[name][2]):
                    raise Held("gbi", f"local scalar {name} differs from the shared declaration")
                removals.append((start, end))
            elif name in sdk_types:
                if tokens(declaration) != tokens(sdk_types[name][2]):
                    raise Held("gbi", f"local SDK type {name} differs from the shared declaration")
                removals.append((start, end))
        includes += [f'#include "{scalar.name}"', '#include "n64sdk.h"']
    if audio:
        # Require words.w0/w1, directly or through a two-word field type.
        declaration = audio[2]
        indirect = re.search(r"\b(\w+)\s+words\s*;", declaration)
        words = source_types.get(indirect[1]) if indirect else None
        audio_fields = words[2] if words else declaration
        if not packet_shape(declaration, indirect[1] if indirect else None) or (
            words and not packet_shape(audio_fields)
        ):
            raise Held("gbi", "local Acmd storage is not a proven two-word packet")
        removals.append(audio[:2])
        # Awords is supplied by the shared type header; other local spellings
        # can only disappear if used exclusively by this removed Acmd type.
        if (
            words
            and indirect
            and (indirect[1] == "Awords" or len(re.findall(r"\b" + indirect[1] + r"\b", source)) == 2)
        ):
            removals.append(words[:2])
        includes.append('#include "acmd.h"')
    for start, end in sorted(set(removals), reverse=True):
        source = source[:start] + source[end:]
    for pointer in gfx_pointers:
        if direct:
            source = re.sub(
                r"\b" + re.escape(pointer) + r"(\s*(?:\[[^\]\n]+\]\s*\.|->)\s*)(w[01])\b",
                r"" + pointer + r"\1words.\2",
                source,
            )
        if flattened:
            source = re.sub(
                r"\b" + re.escape(pointer) + r"(\s*(?:\[[^\]\n]+\]\s*\.|->)\s*)words_(w[01])\b",
                r"" + pointer + r"\1words.\2",
                source,
            )
    # Scalar declarations must precede n64sdk.h even when their include already
    # occurred later in the original unit. Move only the headers being reused.
    for include in includes:
        source = re.sub(r"^[ \t]*" + re.escape(include) + r"[ \t]*\n?", "", source, flags=re.M)
    return reuse_shared(project, "\n".join(includes) + "\n" + source)
