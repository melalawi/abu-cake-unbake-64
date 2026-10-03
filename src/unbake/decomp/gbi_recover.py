"""Invert active SDK macro definitions, with object proof before accepting edits.

Only packet stores and transparent local builders are candidates. Encodings,
parameter order, shifts and masks come from the project's preprocessed headers.
Unsupported definitions and partial packets are deliberately held.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from unbake.decomp import checks
from unbake.decomp.gbi import LEXICAL, PAIR
from unbake.decomp.gbi_expr import Ambiguous, Word, integer, pure, scalar, split, unwrap
from unbake.decomp.gbi_source import (
    Macro,
    initializer_pairs,
    invocations,
    macros,
    packet_pointers,
    tokens,
    word_builder,
)
from unbake.project.config import Held, Policy, Project

RULES = frozenset({"raw-gfx", "local-gbi-macro"})


def expand(text: str, definitions: dict[str, Macro], objects: dict[str, str], depth: int = 0) -> str:
    """Expand helpers while retaining the SDK's bit-field primitive."""
    if depth > 20:
        raise Ambiguous("recursive SDK macro")
    for name in set(re.findall(r"\b\w+\b", text)) & definitions.keys():
        if name in {"_SHIFTL", "_SHIFTR"}:
            continue
        macro = definitions[name]
        for start, end, args in reversed(invocations(text, name)):
            replacement = macro.substitute(args)
            if replacement is None:
                raise Ambiguous("unsupported macro substitution")
            text = text[:start] + expand(replacement, definitions, objects, depth + 1) + text[end:]
    updated = re.sub(r"\b\w+\b", lambda m: "(" + objects[m[0]] + ")" if m[0] in objects else m[0], text)
    return expand(updated, definitions, objects, depth + 1) if updated != text else text


def inverse(expression: str, value: str, parameters: set[str]) -> tuple[str, str]:
    """Solve one parameter through a field's literal arithmetic operations."""
    expression = scalar(expression)
    if expression in parameters:
        return expression, unwrap(value)
    for op in ("+", "-", "*", "/", "<<", ">>", "^"):
        parts = split(expression, op)
        if len(parts) > 2 and op in {"+", "-"}:
            parts = [f" {op} ".join(parts[:-1]), parts[-1]]
        if len(parts) != 2:
            continue
        right, left = integer(parts[1]), integer(parts[0])
        if right is not None:
            undo = {"+": "-", "-": "+", "*": "/", "/": "*", "<<": ">>", ">>": "<<", "^": "^"}[op]
            if op in {"*", "/"} and right == 0:
                break
            fixed = integer(value)
            if op == "*" and fixed is not None and fixed % right:
                break
            solved = f"({value}) {undo} {right}"
            numeric = fixed // right if op == "*" and fixed is not None else integer(solved)
            return inverse(parts[0], str(numeric) if numeric is not None else solved, parameters)
        if left is not None and op == "-":
            solved = f"{left} - ({value})"
            numeric = integer(solved)
            return inverse(parts[1], str(numeric) if numeric is not None else solved, parameters)
        if left is not None and op in {"+", "*"}:
            return inverse(f"({parts[1]}) {op} {left}", value, parameters)
    raise Ambiguous("SDK field is not invertible")


def literal(text: str) -> int | None:
    """Evaluate literal-only left-associative SDK arithmetic."""
    text = scalar(text)
    fixed = integer(text)
    if fixed is not None:
        return fixed
    for op in ("|", "^", "&", "<<", ">>", "+", "-", "*", "/"):
        parts = split(text, op)
        if len(parts) < 2:
            continue
        values = [literal(part) for part in parts]
        if any(value is None for value in values):
            continue
        result = values[0]
        assert result is not None
        for value in values[1:]:
            assert value is not None
            if op == "/":
                if value == 0:
                    return None
                result = abs(result) // abs(value) * (-1 if (result < 0) != (value < 0) else 1)
            else:
                updated = integer(f"({result}) {op} ({value})")
                if updated is None:
                    return None
                result = updated
        return result
    return None


def word(text: str) -> Word:
    """Normalize contiguous masks after shifts without changing their widths."""
    for start, end, args in reversed(invocations(text, "_SHIFTL")):
        arguments = [str(fixed) if (fixed := literal(arg)) is not None else arg for arg in args]
        text = text[:start] + f"_SHIFTL({', '.join(arguments)})" + text[end:]
    terms = []
    for term in split(scalar(text), "|"):
        masked = split(scalar(term), "&")
        if len(masked) == 2 and (mask := integer(masked[1])) is not None and mask > 0:
            shifted = split(scalar(masked[0]), "<<")
            if len(shifted) == 2 and (shift := integer(shifted[1])) is not None and 0 <= shift < 32:
                field = mask >> shift
                if field and not field & (field + 1) and mask == field << shift:
                    term = f"_SHIFTL({shifted[0]}, {shift}, {field.bit_length()})"
        terms.append(term)
    return Word.parse(" | ".join(terms))


@dataclass(frozen=True)
class Pattern:
    macro: Macro
    words: tuple[Word, Word]
    expressions: tuple[str, str]
    static: bool = False
    objects: dict[str, str] = field(default_factory=dict)

    def match(self, w0: str, w1: str) -> list[str]:
        bindings: dict[str, str] = {}
        parameters = set(self.macro.parameters if self.static else self.macro.parameters[1:])
        for expected, raw in zip(self.words, (w0, w1), strict=True):
            whole = len(expected.parts) == 1 and expected.parts[0].shift == 0 and expected.parts[0].width == 32
            layout_mask = sum(part.mask for part in expected.parts)
            projected = False
            try:
                actual = Word() if whole else word(raw)
            except Ambiguous:
                if layout_mask != 0xFFFFFFFF or expected.constant or not pure(scalar(raw)):
                    raise
                actual = Word()
                projected = True
            if (
                not whole
                and layout_mask == 0xFFFFFFFF
                and not expected.constant
                and any(
                    any(
                        raw_part.mask & part.mask and (raw_part.shift, raw_part.width) != (part.shift, part.width)
                        for part in expected.parts
                    )
                    for raw_part in actual.parts
                )
            ):
                actual = Word()
                projected = True
            field_mask = 0
            for part in sorted(expected.parts, key=lambda p: len(set(re.findall(r"\b\w+\b", p.value)) & parameters)):
                expression = re.sub(
                    r"\b\w+\b", lambda m: f"({bindings[m[0]]})" if m[0] in bindings else m[0], part.value
                )
                field_mask |= part.mask
                if part.shift == 0 and part.width == 32:
                    if not pure(scalar(raw)):
                        raise Ambiguous("packet operand has effects")
                    value = scalar(raw)
                    actual.used = 0xFFFFFFFF
                elif projected:
                    value = f"((unsigned int)({raw}) >> {part.shift}) & {(1 << part.width) - 1}"
                else:
                    value = actual.take(part.shift, part.width)
                if not (set(re.findall(r"\b\w+\b", expression)) & (parameters - bindings.keys())):
                    if integer(expression) != integer(value) and tokens(scalar(expression)) != tokens(scalar(value)):
                        raise Ambiguous("SDK parameter has inconsistent fields")
                    continue
                parameter, argument = inverse(expression, value, parameters - bindings.keys())
                if parameter in bindings and tokens(bindings[parameter]) != tokens(argument):
                    raise Ambiguous("SDK parameter has inconsistent fields")
                bindings[parameter] = argument
            if any(part.mask & ~field_mask for part in actual.parts):
                raise Ambiguous("operand can spill into SDK fixed bits")
            if expected.constant & field_mask & ~actual.constant:
                raise Ambiguous("SDK forced field bits differ")
            if actual.constant & ~field_mask != expected.constant & ~field_mask:
                raise Ambiguous("SDK fixed bits differ")
            actual.used |= ~field_mask & 0xFFFFFFFF
            actual.finish()
        return [bindings.get(name, "0") for name in self.macro.parameters if name in parameters]


def patterns(definitions: str) -> list[Pattern]:
    """Build a catalogue from cpp -dM output, never from opcode/name tables."""
    functions = macros(definitions)
    objects = {
        m[1]: m[2].strip()
        for m in re.finditer(r"^#\s*define\s+(\w+)[ \t]+([^\n]+)$", definitions, re.M)
        if not m[2].strip().startswith(('"', "'"))
    }
    result = []
    for name, macro in functions.items():
        if not re.fullmatch(r"g(?:s)?[DS]P\w+", name):
            continue
        try:
            body = expand(macro.body, functions, objects)
            static = name.startswith("gs")
            if static:
                pair = split(body.strip("{} \t"), ",")
                if len(pair) != 2:
                    continue
                expressions = pair[0], pair[1]
            else:
                builder = word_builder(Macro(name, macro.parameters, body, 0, 0), {})
                if builder is None or unwrap(re.sub(r"\(\s*Gfx\s*\*\s*\)", "", builder[0])) != macro.parameters[0]:
                    continue
                expressions = builder[1], builder[2]
            words = word(expressions[0]), word(expressions[1])
            # A selector-taking bit bucket is not a recovered command family.
            # Require the SDK definition itself to fix the command tag.
            if any(part.mask & 0xFF000000 for part in words[0].parts):
                continue
            result.append(Pattern(macro, words, expressions, static, objects))
        except (Ambiguous, IndexError):
            continue
    # Prefer narrower, semantic wrappers over parameterized opcode builders.
    return sorted(
        result,
        key=lambda p: (
            -sum(part.constant.bit_count() for part in p.words),
            len(p.macro.parameters),
            p.macro.name,
        ),
    )


def select(catalogue: list[Pattern], pointer: str | None, w0: str, w1: str) -> str:
    for pattern in catalogue:
        if pattern.static != (pointer is None):
            continue
        try:
            args = pattern.match(w0, w1)
        except Ambiguous:
            continue
        return f"{pattern.macro.name}({', '.join(args if pointer is None else [pointer, *args])})"
    raise Ambiguous("no SDK macro matches the ordered word fields")


def lower(source: str, catalogue: list[Pattern]) -> str:
    """Plan an all-or-nothing packet rewrite, leaving non-GBI bytes untouched."""
    definitions = macros(source)
    objects = catalogue[0].objects if catalogue else {}
    objects = {**objects, **{m[1]: m[2] for m in re.finditer(r"^\s*#\s*define\s+(\w+)[ \t]+([^\n]+)$", source, re.M)}}
    builders = dict(definitions)
    for match in re.finditer(r"\bstatic\s+(?:inline\s+)?void\s+(\w+)\s*\(([^(){}]*)\)\s*\{([^{}]*)\}", source):
        parameters = []
        for declaration in split(match[2], ","):
            parameter_name = re.search(r"\b(\w+)\s*$", declaration)
            if parameter_name and parameter_name[1] != "void":
                parameters.append(parameter_name[1])
        body = re.sub(
            r"\bGfx\s*\*\s*(\w+)\s*;\s*\1\s*=",
            r"Gfx *\1 =",
            match[3],
        )
        builders[match[1]] = Macro(match[1], parameters, body, match.start(), match.end())
    masked = LEXICAL.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), source)
    audio_pointers = packet_pointers(source, "Acmd")
    replacements: list[tuple[int, int, str]] = []
    local_builders: set[str] = set()
    for macro in builders.values():
        try:
            body = expand(macro.body, definitions, {})
        except Ambiguous:
            continue
        builder = word_builder(Macro(macro.name, macro.parameters, body, macro.start, macro.end), {})
        if builder is None:
            continue
        proposed = []
        failed = False
        sites = [site for site in invocations(masked, macro.name) if not macro.start <= site[0] < macro.end]
        if not sites:
            continue
        for start, end, args in sites:
            if len(macro.parameters) != len(args):
                failed = True
                break
            pointer_parameters = set(re.findall(r"\b\w+\b", builder[0])) & set(macro.parameters)
            if any(
                not pure(scalar(expand(arg, definitions, objects))) and parameter not in pointer_parameters
                for parameter, arg in zip(macro.parameters, args, strict=True)
            ):
                failed = True
                break
            bindings = dict(zip(macro.parameters, args, strict=True))

            def substitute(text: str, bindings: dict[str, str] = bindings) -> str:
                return re.sub(r"\b\w+\b", lambda m: f"({bindings[m[0]]})" if m[0] in bindings else m[0], text)

            pointer = substitute(builder[0])
            w0, w1 = (expand(substitute(value), definitions, objects) for value in builder[1:])
            try:
                proposed.append((start, end, select(catalogue, pointer, w0, w1)))
            except Ambiguous:
                failed = True
                break
        if not failed:
            local_builders.add(macro.name)
            replacements.extend(proposed)
            replacements.append((macro.start, macro.end, ""))
    for match in PAIR.finditer(masked):
        if match["middle"].strip():
            continue
        start, end = match.span()
        if re.search(r"^\s*#", source[start:end], re.M):
            continue
        before = masked[masked.rfind("\n", 0, start) + 1 : start]
        if re.search(r"\b(?:if|while|for)\s*\([^{};]*\)\s*$", before):
            continue
        ptr = match["ptr"].strip()
        if ptr in audio_pointers:
            continue
        if not pure(ptr) or re.search(r"\bvolatile\b", source[:start]):
            continue
        values = [expand(source[match.start(key) : match.end(key)], definitions, objects) for key in ("w0", "w1")]
        try:
            call = select(catalogue, ptr if match["access"] == "->" else f"&{ptr}", *values)
        except Ambiguous:
            continue
        comments = re.findall(r"/\*.*?\*/|//[^\n]*", source[start:end], re.S)
        replacements.append((start, end, "\n".join([*comments, call + ";"])))
    for start, end, _w0, _w1 in initializer_pairs(masked):
        # The masked view has the same offsets; take operands from authored bytes.
        pair = split(source[start:end].strip("{} \t\n"), ",")
        if len(pair) != 2:
            continue
        try:
            call = select(catalogue, None, *(expand(value, definitions, objects) for value in pair))
        except Ambiguous:
            continue
        replacements.append((start, end, call))
    sdk_names = {pattern.macro.name for pattern in catalogue}
    for macro in definitions.values():
        if macro.name in local_builders:
            continue
        body = macro.body.strip().strip("{} ;\t\n")
        calls = [site for name in sdk_names - {macro.name} for site in invocations(body, name)]
        if len(calls) != 1 or calls[0][0] != 0 or calls[0][1] != len(body):
            continue
        sites = invocations(masked, macro.name)
        if not sites or any(len(args) != len(macro.parameters) for _, _, args in sites):
            continue
        for start, end, args in sites:
            replacement = macro.substitute(args)
            assert replacement is not None
            replacements.append((start, end, replacement.strip().strip("{} ;\t\n")))
        replacements.append((macro.start, macro.end, ""))
        local_builders.add(macro.name)
    for name, macro in definitions.items():
        if name in local_builders:
            continue
        if name not in sdk_names and name not in {"_SHIFTL", "_SHIFTR"}:
            continue
        # Retire only a byte/token-equivalent definition, not an opcode imitation.
        if name in sdk_names:
            sdk = next(pattern.macro for pattern in catalogue if pattern.macro.name == name)
            if macro.parameters != sdk.parameters or tokens(macro.body) != tokens(sdk.body):
                continue
        else:
            from unbake.decomp.gbi_source import standard_shiftl

            if name != "_SHIFTL" or not standard_shiftl(macro):
                continue
        replacements.append((macro.start, macro.end, ""))
    for start, end, text in sorted(replacements, reverse=True):
        source = source[:start] + text + source[end:]
    blockers = [finding for finding in checks.run(source) if finding.rule in RULES]
    if blockers:
        first = min(blockers, key=lambda f: f.line)
        line = source.splitlines()[first.line - 1].strip()
        raise Held("gbi", f"SDK macro recovery: first unmatched write {checks.message(first)}: {line}")
    return source


def catalogue(project: Project, policy: Policy, unit: Path, version: str, source: str) -> list[Pattern]:
    """Read active SDK definitions under the unit's configured preprocessor flags."""
    from unbake.decomp.trial_compile import run_tool
    from unbake.project import makefile

    recipe = makefile.recipe(project)
    flags = [*recipe.cppflags, *makefile.flags(project, version, project.src / unit.name)]
    options: list[str] = []
    iterator = iter(flags)
    for flag in iterator:
        if flag in {"-D", "-U", "-I"}:
            options.extend((flag, next(iterator, "")))
        elif flag.startswith(("-D", "-U", "-I")):
            options.append(flag)
    headers = [
        path
        for root in project.include
        for path in root.rglob("*.h")
        if re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", path.read_text(), re.M)
    ]
    if not headers:
        raise Held("gbi", "SDK macro recovery: raw-gfx/local-gbi-macro requires project SDK macro definitions")
    directives = []
    for line in source.splitlines():
        included = re.match(r'\s*#\s*include\s*[<"]([^>"]+)[>"]', line)
        if included:
            provider = next(
                (
                    root / included[1]
                    for root in (*project.include, *getattr(project, "declaration_evidence", ()))
                    if (root / included[1]).is_file()
                ),
                None,
            )
            if provider is not None and re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", provider.read_text(), re.M):
                break
        if re.match(r"\s*#\s*(?:define|undef)\b", line):
            directives.append(line)
    with tempfile.TemporaryDirectory(prefix="gbi-sdk-") as temporary:
        work = Path(temporary)
        probe = work / "sdk.c"
        probe.write_text("\n".join([*directives, *(f'#include "{path}"' for path in sorted(headers))]) + "\n")
        command = [
            makefile.host_executable(policy, recipe.cpp or "", "cpp"),
            "-dM",
            "-undef",
            "-nostdinc",
            *(f"-I{root}" for root in project.include),
            *options,
            str(probe),
        ]
        from unbake.project.cache import remembered

        definitions = run_tool(command, work, "gbi")
        return remembered("gbi.sdk.patterns", definitions, lambda: patterns(definitions))


def proven(
    project: Project,
    policy: Policy,
    unit: Path,
    source: str,
    headers: dict[Path, str],
    *,
    authored: str | None = None,
) -> str:
    """Keep a private rewrite only if every owning VERSION and mode is identical."""
    if not any(f.rule in RULES for f in checks.run(source)):
        return source
    from unbake.decomp import gbi_proof, work
    from unbake.layout import split as layout_split

    before = import_aliases(project, source, headers, sdk_aliases=False)
    # Provider resolution may have added the installed SDK before a missing
    # legacy include. Restore the authored SDK choice for the raw comparison.
    from unbake.match.imports import _INCLUDE, _without_comments

    requested = set(_INCLUDE.findall(_without_comments(authored if authored is not None else source)))
    installed = {
        path.relative_to(root).as_posix()
        for path, text in headers.items()
        for root in project.include
        if path.is_relative_to(root)
        if re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", text, re.M)
    }
    legacy = any(
        (old / name).is_file() and re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", (old / name).read_text(), re.M)
        for name in requested - installed
        for old in getattr(project, "declaration_evidence", ())
    )
    if legacy and not requested & installed:
        before = _INCLUDE.sub(lambda match: "" if match[1] in installed else match[0], before)
    source = import_aliases(project, source, headers)

    with tempfile.TemporaryDirectory(prefix="gbi-recovery-") as temporary:
        root = Path(temporary)
        staged = work.overlay(project, root)
        for path, text in headers.items():
            destination = root / "overlay" / path.relative_to(project.root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
        versions = layout_split.holding_versions(project, unit.stem)
        recovered = [lower(source, catalogue(staged, policy, unit, version, source)) for version in versions]
        if not recovered or any(text != recovered[0] for text in recovered):
            raise Held("gbi", f"{unit.stem}: SDK macro recovery differs between owning VERSIONs")
        from unbake.layout.header_context import Headers
        from unbake.match import imports

        after = imports.resolve(staged, Headers.read(staged), recovered[0], unit.stem)
        # The raw form sees its actual legacy SDK macros, not the replacement
        # header. Only private proof inputs receive these read-only bytes.
        for name in _INCLUDE.findall(_without_comments(before)):
            if any((include / name).is_file() for include in staged.include):
                continue
            evidence = next(
                (old / name for old in getattr(project, "declaration_evidence", ()) if (old / name).is_file()), None
            )
            if evidence is not None and re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", evidence.read_text(), re.M):
                destination = staged.include[0] / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(evidence.read_bytes())
        try:
            gbi_proof.preserve(staged, policy, unit, before, after)
        except Held as error:
            first = next(f for f in checks.run(source) if f.rule in RULES)
            raise Held(
                "gbi", f"SDK macro recovery: first unmatched write {checks.message(first)}; {error.reason}"
            ) from error
        return after


def preflight(project: Project, policy: Policy, unit: Path, source: str) -> None:
    """Name an unrepresentable write before expensive declaration preparation."""
    if not any(f.rule in RULES for f in checks.run(source)):
        return
    from unbake.layout import split as layout_split

    for version in layout_split.holding_versions(project, unit.stem):
        lower(source, catalogue(project, policy, unit, version, source))


def import_aliases(project: Project, source: str, headers: dict[Path, str], *, sdk_aliases: bool = True) -> str:
    """Replace absent SDK/scalar imports with equivalent installed providers.

    Evidence is read only. General authored headers, structures, pragmas and
    function declarations are not aliases and remain for ordinary diagnostics.
    """
    if not any(f.rule in RULES for f in checks.run(source)):
        return source
    from unbake.match.imports import _INCLUDE, _without_comments

    scalar_decl = re.compile(r"\btypedef\s+((?:(?:unsigned|signed|char|short|int|long|float|double)\s+)+)(\w+)\s*;")

    def scalars(text: str) -> dict[str, str]:
        return {
            m[2]: re.sub(r"\bsigned (?=(?:short|int|long)\b)", "", " ".join(m[1].split()))
            for m in scalar_decl.finditer(text)
        }

    sdk = [path for path, text in headers.items() if re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", text, re.M)]
    for match in reversed(list(_INCLUDE.finditer(_without_comments(source)))):
        name = match[1]
        if any(root / name in headers for root in project.include):
            continue
        evidence = next(
            (root / name for root in getattr(project, "declaration_evidence", ()) if (root / name).is_file()), None
        )
        if evidence is None:
            continue
        text = _without_comments(evidence.read_text())
        candidate = None
        if re.search(r"^\s*#\s*define\s+g(?:s)?[DS]P\w+\(", text, re.M):
            if sdk_aliases and len(sdk) == 1:
                candidate = sdk[0]
        else:
            aliases = scalars(text)
            remainder = scalar_decl.sub("", text)
            remainder = re.sub(r"^\s*#.*$", "", remainder, flags=re.M).strip()
            used = set(re.findall(r"\b\w+\b", _INCLUDE.sub("", source)))
            object_names = set(re.findall(r"^\s*#\s*define\s+(\w+)(?:\s|$)", text, re.M)) & used
            if aliases and not remainder and not object_names:
                candidate = next(
                    (path for path, body in headers.items() if aliases.items() <= scalars(body).items()), None
                )
        if candidate is not None:
            include = next(
                candidate.relative_to(root).as_posix() for root in project.include if candidate.is_relative_to(root)
            )
            source = source[: match.start()] + f'#include "{include}"' + source[match.end() :]
    return source
