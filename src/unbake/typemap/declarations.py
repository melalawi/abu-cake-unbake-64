"""Explicit C declarations as target-ABI type seeds, without decompiler guesses."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import weakref
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import cdecl, inputs, pool, prefixes, tui
from unbake.cache import memo
from unbake.cdecl import attribute_source, declaration_source
from unbake.cdecl import declarations as header_declarations
from unbake.config import Held, Host, Project
from unbake.decomp.draft_context import ordered_headers
from unbake.project.headers import include_headers
from unbake.typemap import storage, unit_layouts

_BOUNDARY = "extern int __unbake_feedback_boundary;"
# One source's facts parse the same unit up to four times (scoped then full, contracts then definition).
UNIT_MEMO = 2
_C_TOKEN = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\'|^[ \t]*#(?:\\\n|[^\n])*|[A-Za-z_]\w*|\S',
    re.M | re.S,
)


def _outer_guard(text: str) -> str | None:
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
        lambda m: " " if m[0].startswith(("/*", "//")) else m[0],
        text,
        flags=re.S,
    )
    guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", text)
    if guard is None or not re.search(r"#\s*endif[^\n]*\s*\Z", text):
        return None
    depth = 0
    directives = list(re.finditer(r"^\s*#\s*(if|ifdef|ifndef|endif|else|elif)\b", text, re.M))
    for index, directive in enumerate(directives):
        if directive[1] in {"else", "elif"}:
            if depth == 1:
                return None
        else:
            depth += 1 if directive[1] != "endif" else -1
        if depth == 0 and index != len(directives) - 1:
            return None
    return guard[1] if depth == 0 else None


BOUNDARY = _BOUNDARY


def cleaned_unit(text: str) -> str:
    """A preprocessed text as a whole-unit extraction parses it: line markers kept, bodies blanked."""
    return _declaration_unit(_unit_clean("published", text, line_markers=True))


def unit_tree(cleaned: str) -> Any:
    """The parse of a cleaned unit, resumed after a prefix shared with a recent unit."""
    return _tree(cleaned, {})


def node_type(node: Any) -> str:
    return _type(node)


def resolver(aliases: dict[str, str]) -> Callable[[str], str]:
    """canonical() bound to ALIASES, remembered per alias environment."""
    return _canonical_in(aliases)


def layout_rows(cleaned: str, aliases: dict[str, str]) -> tuple[dict[str, Any], list[str], dict[str, int]]:
    """Layout records of a cleaned unit (no provenance), its layout refusal if any, and the line (from 0) where
    each record's aggregate is defined."""
    import bisect

    layout_source = _unit_clean("layouts", cleaned, line_markers=False)
    rows, unknown, starts = unit_layouts.mentioned(layout_source, aliases)
    breaks = [index for index, char in enumerate(layout_source) if char == "\n"]
    return rows, unknown, {name: bisect.bisect_left(breaks, start) for name, start in starts.items()}


def _declaration_unit(source: str) -> str:
    """_unit_bodies_blanked, once per text, resumed after the header prefix shared with recent units."""
    return memo(
        "decl.unit",
        source,
        lambda: prefixes.concatenated("unit.blank", source, _unit_bodies_blanked),
        size=retention.memory_size,
        copy_out=retention.clone,
    )


def source_definition_units(
    source: Path, text: str, project: Project | None = None, policy: Host | None = None
) -> Iterator[str]:
    """File-scope definition views, selecting configured versions for conditional braces.

    Mutually exclusive branches may each open a block closed by shared code.
    Only the actual preprocessor environment can distinguish that from a
    malformed body; never suppress the refusal or concatenate those branches.
    """
    conditional_depth = 0
    conditional_braces = False
    for match in _C_TOKEN.finditer(text):
        token = match[0]
        directive = re.match(r"^[ \t]*#\s*(if|ifdef|ifndef|endif)\b", token)
        if directive:
            conditional_depth += -1 if directive[1] == "endif" else 1
        elif conditional_depth and token in {"{", "}"}:
            conditional_braces = True
            break
    if not conditional_braces or project is None or policy is None:
        try:
            unit = _declaration_unit(declaration_source(text))
        except Held as error:
            raise Held(error.phase, f"types.declaration: {source}: {error.reason}") from error
        if conditional_braces:
            raise Held("solve", f"types.declaration: {source}: conditional braces require a preprocessor environment")
        yield unit
        return
    from unbake.fold.source_views import active_source

    seen: set[str] = set()
    for version in project.versions:
        try:
            view = active_source(project, policy, text, version, source.stem)
            masked = declaration_source(view)
            if masked in seen:
                continue
            seen.add(masked)
            unit = _declaration_unit(masked)
        except Held as error:
            raise Held(error.phase, f"types.declaration: {source}: {version}: {error.reason}") from error
        yield unit


def _unit_clean(stream: str, source: str, *, line_markers: bool) -> str:
    """clean() of a whole unit, once per text, resumed after the header prefix shared with recent units."""
    return memo(
        "decl.clean." + stream,
        (source, line_markers),
        lambda: prefixes.concatenated(
            f"unit.clean.{stream}.{line_markers}", source, lambda text: clean(text, line_markers=line_markers)
        ),
        size=retention.memory_size,
        copy_out=retention.clone,
    )


def _unit_bodies_blanked(source: str) -> str:
    """Keep function definitions' signatures, never parse their implementation.

    Proven source code may contain label addresses, computed goto or inline asm.
    None contributes to declaration evidence. Blank bodies without moving line
    markers; aggregate definitions and file-scope initializers remain intact.
    """
    tokens = iter(_C_TOKEN.finditer(source))
    spans: list[tuple[int, int]] = []
    depth = parens = 0
    assigned = False
    previous = ""
    statement_start = 0
    for match in tokens:
        token = match[0]
        if token.startswith(("#", "/*", "//")):
            continue
        if token == "{" and depth == 0 and previous == ")" and not assigned:
            begin, level = match.end(), 1
            for closing in tokens:
                word = closing[0]
                if word.startswith(("#", "/*", "//")):
                    continue
                level += (word == "{") - (word == "}")
                if level == 0:
                    spans.append((begin, closing.start()))
                    break
            else:
                line = source.count("\n", 0, match.start()) + 1
                context = cdecl.located(source, f":{line}:1: unclosed function body")
                signature = re.sub(r"\s+", " ", source[statement_start : match.start()]).strip()[-200:]
                raise Held("solve", f"types.declaration: {context}: function {signature}")
            assigned, previous = False, "}"
            statement_start = closing.end()
            continue
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            parens += (token == "(") - (token == ")")
            if not parens:
                assigned |= token == "="
                if token == ";":
                    assigned = False
                    statement_start = match.end()
        previous = token
    pieces: list[str] = []
    cursor = 0
    for begin, end in spans:
        pieces.extend((source[cursor:begin], re.sub(r"[^\n]", " ", source[begin:end])))
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces)


def clean(source: str, *, line_markers: bool = False) -> str:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", " ", source, flags=re.S)
    source = re.sub(r"^\s*#(?!\s*\d+\s+\")[^\n]*" if line_markers else r"^\s*#[^\n]*", "", source, flags=re.M)
    source = re.sub(r"\b(?:__extension__|__inline__|__inline|__restrict|restrict)\b", "", source)
    return attribute_source(source)


def headers(
    project: Project,
    policy: Host | None,
    version: str,
    extra: Path | None = None,
    *,
    line_markers: bool = False,
    contents: dict[Path, str] | None = None,
) -> str:
    if contents is None:
        contents = {
            path: path.read_text()
            for path, _ in include_headers(project, exclude=lambda path: storage.generated(project, path))
            if not storage.generated(project, path)
        }
    if extra is None:
        from unbake.cache import memo

        selection = (
            project.root,
            version,
            line_markers,
            None if policy is None else (str(policy.cpp), project.cppflags),
            project.compilers[project.default_compiler].cflags,
            project.include,
            project.version(version).macros,
            tuple(sorted(contents.items())),
        )
        return memo(
            "typemap.headers",
            selection,
            lambda: _headers(project, policy, version, contents, None, line_markers=line_markers),
            size=retention.memory_size,
            copy_out=retention.clone,
        )
    return _headers(project, policy, version, contents, extra, line_markers=line_markers)


def _generated_context(project: Project) -> list[Path]:
    if not project.include:
        return []
    from unbake.layout import index

    return sorted(index.headers(project))


def _headers(
    project: Project,
    policy: Host | None,
    version: str,
    contents: dict[Path, str],
    extra: Path | None,
    *,
    line_markers: bool,
    ordered: list[Path] | None = None,
    raw: bool = False,
    include_generated: bool = True,
) -> str:
    if ordered is None:
        ordered = ordered_headers(contents)
    if policy is None:
        # Raw guarded headers are useful to in-memory callers; other conditionals need cpp.
        for path, text in contents.items():
            if re.search(r"^\s*#\s*(?:if\b|elif\b|else\b)", text, re.M):
                raise Held("solve", f"types.declaration: {path}: policy.cpp required for conditional types")
        if extra is not None:
            if re.search(r"^\s*#\s*(?:if\b|ifdef\b|ifndef\b|elif\b|else\b)", extra.read_text(), re.M):
                raise Held("solve", f"types.declaration: {extra}: policy.cpp required for conditional C")
            text = "\n".join(contents[path] for path in ordered) + "\n"
            text += (_BOUNDARY + "\n" if raw else "") + extra.read_text()
            return text if raw else clean(text)
        if line_markers:
            return "\n".join(f'# 1 "{path}"\n' + clean(contents[path]) for path in ordered)
        return clean("\n".join(contents[path] for path in ordered))
    if not policy.cpp:
        raise Held("solve", "policy.cpp: required for typed header preprocessing")
    source = "".join(f'#include "{path}"\n' for path in ordered)
    if extra is not None:
        if include_generated:
            source += "".join(f'#include "{path}"\n' for path in _generated_context(project))
        if raw:
            source += _BOUNDARY + "\n"
        source += f'#include "{extra}"\n'
    command = _cpp_command(
        project,
        policy,
        version,
        extra=extra is not None,
        line_markers=line_markers,
        unit=extra.stem if extra is not None and extra.suffix == ".c" else None,
    )
    text = _preprocess(project, command, source)
    return text if raw else clean(text, line_markers=extra is not None or line_markers)


def _cpp_command(
    project: Project, policy: Host, version: str, *, extra: bool, line_markers: bool, unit: str | None = None
) -> list[str]:
    from unbake.compilers import drivers

    compiler = project.compilers[project.default_compiler] if unit is None else project.compiler_for(unit)
    effective = (
        drivers.flags(project, version, unit)
        if unit is not None
        else [
            *(f"-I{root}" for root in project.include),
            *compiler.cflags,
            *(f"-D{macro}" for macro in project.version(version).macros),
        ]
    )
    preprocess, _ = drivers.stage_flags(compiler.id, effective)
    options = [*preprocess, *(("-DUNBAKE_PROTOTYPES_H",) if extra else ())]
    return drivers.context_command(project, str(policy.cpp), compiler, options, line_markers=line_markers)


def _preprocess(project: Project, command: list[str], source: str) -> str:
    from unbake.process import run_tool

    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".headers-", dir=project.build) as temporary:
        wrapper = Path(temporary) / "context.c"
        atomic_files.fresh(wrapper, source.encode())
        return run_tool([*command[:-1], str(wrapper)], project.root, "solve", temporary_root=project.build).replace(
            str(wrapper), "<unbake-context>"
        )


def source_unit(
    project: Project, policy: Host | None, version: str, source: Path, *, line_markers: bool = False
) -> str:
    """One source preprocessed with only its own includes, split by the source boundary."""
    if policy is None:
        return _headers(
            project,
            policy,
            version,
            {},
            source,
            line_markers=line_markers,
            ordered=[],
            raw=True,
            include_generated=False,
        )
    from unbake.compilers import drivers
    from unbake.process import run_tool

    unit = source.stem
    # A header uses the explicitly configured compiler's contract, never a guessed C unit.
    if source.suffix != ".c":
        command = _cpp_command(project, policy, version, extra=True, line_markers=line_markers)
        return _preprocess(project, command, _BOUNDARY + "\n" + f'#include "{source}"\n')
    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".unit-", dir=project.build) as temporary:
        wrapper = Path(temporary) / "unit.c"
        atomic_files.fresh(wrapper, (_BOUNDARY + "\n" + f'#include "{source}"\n').encode())
        command = drivers.preprocess_command(
            project, str(policy.cpp), version, unit, wrapper, non_matching=False, line_markers=line_markers
        )
        return run_tool(command, project.root, "solve", temporary_root=project.build).replace(
            str(wrapper), "<unbake-unit>"
        )


class ProvenStructs(Mapping[str, Any]):
    """A shared immutable layout template with this receipt's provenance."""

    def __init__(self, template: dict[str, Any], provenance: dict[str, Any]) -> None:
        self.template, self.provenance = template, provenance

    def __len__(self) -> int:
        return len(self.template)

    def __iter__(self) -> Iterator[str]:
        return iter(self.template)

    def __getitem__(self, name: str) -> Any:
        return {**self.template[name], "provenance": self.provenance}


class _FullDeclarationUnit(Exception):
    """A source changes the layout meaning of its shared header prefix."""


_TYPES: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_PROTOTYPES: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def _type(node: Any) -> str:
    """_node_type once per node: a resumed parse shares its header prefix's nodes across units."""
    found = _TYPES.get(node)
    if found is None:
        found = _TYPES[node] = _node_type(node)
    return found


def _prototype(declaration: Any) -> str:
    found = _PROTOTYPES.get(declaration)
    if found is None:
        found = _PROTOTYPES[declaration] = c_generator.CGenerator().visit(declaration) + ";"
    return found


_GLOBALS: weakref.WeakKeyDictionary[Any, tuple[str, str, tuple[str, str | None] | None]] = weakref.WeakKeyDictionary()


def _global(declaration: Any) -> tuple[str, str, tuple[str, str | None] | None]:
    """A global's type, extern declaration and array element/extent, once per node."""
    found = _GLOBALS.get(declaration)
    if found is None:
        generator = c_generator.CGenerator()
        # A shallow copy: the parse tree is shared, and only the copy drops its initializer.
        bare = copy.copy(declaration)
        bare.init = None
        array = None
        if isinstance(declaration.type, c_ast.ArrayDecl):
            dim = declaration.type.dim
            array = _type(declaration.type.type), None if dim is None else generator.visit(dim)
        extern = "extern " + generator.visit(bare).removeprefix("extern ") + ";"
        found = _GLOBALS[declaration] = _type(declaration.type), extern, array
    return found


def _node_type(node: Any) -> str:
    # A named aggregate's body never enters an abstract type spelling. Prune
    # it before copying; callbacks can otherwise copy a whole shared layout.
    memo: dict[int, Any] = {}

    def names(value: Any) -> None:
        if isinstance(value, (c_ast.Struct, c_ast.Union)) and value.name:
            retained = copy.copy(value)
            retained.decls = None
            memo[id(value)] = retained
        else:
            for _, child in value.children():
                names(child)

    names(node)
    node = copy.deepcopy(node, memo)

    class Anonymous(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_TypeDecl(self, value: Any) -> None:
            value.declname = None
            self.generic_visit(value)

        def visit_Struct(self, value: Any) -> None:
            if value.name:
                value.decls = None

        visit_Union = visit_Struct

    Anonymous().visit(node)
    return " ".join(c_generator.CGenerator().visit(node).split())


def canonical(type_: str, aliases: dict[str, str]) -> str:
    previous = set()
    while type_ not in previous:
        previous.add(type_)
        changed = re.sub(
            r"\b(?:struct|union|enum)\s+[A-Za-z_]\w*|\b[A-Za-z_]\w*\b",
            lambda m: aliases.get(m[0], m[0]),
            type_,
        )
        if changed == type_:
            break
        type_ = changed
    type_ = re.sub(r"\s*\*\s*", " *", type_).strip()
    return {
        "signed": "int",
        "signed int": "int",
        "signed short": "short",
        "signed short int": "short",
        "signed long": "long",
        "signed long int": "long",
        "signed long long": "long long",
        "signed long long int": "long long",
        "unsigned": "unsigned int",
        "short int": "short",
        "unsigned short int": "unsigned short",
        "long long int": "long long",
        "unsigned long long int": "unsigned long long",
        "long int": "long",
        "unsigned long int": "unsigned long",
    }.get(type_, type_)


def unknown(type_: str) -> bool:
    """Decompiler placeholders specify machine widths, not semantic C types."""
    return bool(re.search(r"\bM2C_(?:UNK|UNKNOWN)\w*\b", type_))


def _canonical_in(aliases: dict[str, str]) -> Callable[[str], str]:
    """canonical(type_, aliases), remembered per alias environment: a unit's extractions share one."""
    bindings = dict(aliases)
    environment = tuple(sorted(bindings.items()))
    known: dict[str, str] = {}

    def resolve(type_: str) -> str:
        found = known.get(type_)
        if found is None:
            found = known[type_] = memo(
                "decl.canonical",
                (environment, type_),
                lambda: canonical(type_, bindings),
                size=retention.memory_size,
                copy_out=str,
            )
        return found

    return resolve


def parameter_registers(params: list[dict[str, Any]], aliases: dict[str, str]) -> list[str | None]:
    return _registers(params, lambda type_: canonical(type_, aliases))


def _registers(params: list[dict[str, Any]], resolve: Callable[[str], str]) -> list[str | None]:
    """O32 first four words, including the leading floating-point register rule."""
    result: list[str | None] = []
    slot = 0
    floating_prefix = True
    for param in params:
        type_ = resolve(param["type"])
        width = 2 if type_ in ("double", "long long", "unsigned long long") else 1
        if width == 2:
            slot += slot % 2
        floating = type_ in ("float", "double")
        if floating and floating_prefix and len(result) < 2:
            result.append("f12" if not result else "f14")
        else:
            result.append(f"r{4 + slot}" if slot < 4 else f"stack{slot * 4}")
        floating_prefix &= floating
        slot += width
    return result


def _tree(source: str, scope: dict[str, bool]) -> Any:
    """A parse is pure in its text and seeded typedef scope; consumers copy before they change a node."""
    key = (source, tuple(sorted(scope.items())))
    return memo(
        "decl.tree",
        key,
        lambda: cdecl.resumable_parse(source, scope),
        size=retention.memory_size,
        copy_out=retention.clone,
    )


def extract(
    source: str,
    provenance: dict[str, Any],
    *,
    definitions: bool = False,
    owned_source: Path | None = None,
    authored_headers: set[Path] | None = None,
    _scope: dict[str, bool] | None = None,
    _prefix: dict[str, Any] | None = None,
    _parser: Any = None,
    _compact: bool = False,
    _contracts: bool = False,
) -> dict[str, Any]:
    original = source
    source = _unit_clean("extract", source, line_markers=owned_source is not None or authored_headers is not None)
    source = _declaration_unit(source)
    try:
        tree = _parser.parse(source) if _parser is not None else _tree(source, _scope or {})
    except Exception as error:
        raise Held("solve", f"types.declaration: {provenance}: {cdecl.located(original, str(error))}") from error
    incoming = {node.name: _type(node.type) for node in tree.ext if isinstance(node, c_ast.Typedef)}
    aliases = {} if _prefix is None else _prefix["aliases"] if _compact and not incoming else dict(_prefix["aliases"])
    aliases.update(incoming)
    shared_typedefs = (
        _prefix["shared_typedefs"]
        if _prefix is not None and _compact and not incoming
        else dict((_prefix or {}).get("shared_typedefs", {}))
    )
    shared_typedefs.update(
        (node.name, incoming[node.name])
        for node in tree.ext
        if isinstance(node, c_ast.Typedef)
        and (owned_source is None or (node.coord.file and node.coord.file != str(owned_source)))
    )
    prefix_structs = {} if _prefix is None else _prefix["structs"]
    resolve = _canonical_in(aliases)
    complete_layout = False
    if _prefix is not None:
        additions = []
        overrides = bool(incoming and _prefix["unknown"])
        for name, type_ in incoming.items():
            if name in _prefix["aliases"]:
                if type_ != _prefix["aliases"][name]:
                    overrides = True
                continue
            target = resolve(type_)
            aggregate = re.fullmatch(r"(?:struct|union) (\w+)", target)
            if aggregate is not None:
                additions.append((aggregate[1], name))
            elif target.startswith(("struct {", "union {", "enum ")):
                overrides = True
        if overrides:
            # The layouts of this unit's whole text: they include the unit's own (and, with an empty prefix, its
            # headers'), so they are never shared with another unit that only spells the same typedefs.
            prefix_structs, unknown = _layout_records(_prefix["layout_source"] + source, {}, aliases)
            if unknown:
                raise _FullDeclarationUnit
            complete_layout = True
        elif additions:
            variants = _prefix.setdefault("layout_aliases", {})
            selection = tuple(additions)
            if selection not in variants:
                changed = dict(prefix_structs)
                for tag, name in additions:
                    if tag in changed:
                        row = changed[tag]
                        changed[tag] = {**row, "aliases": [*row["aliases"], name]}
                variants[selection] = changed
            prefix_structs = variants[selection]
    result: dict[str, Any] = {
        "functions": {},
        "globals": {},
        "structs": {},
        "arrays": {},
        "aliases": aliases,
        "shared_typedefs": shared_typedefs,
        "unknown": [],
    }
    if _prefix is not None:
        for kind in ("functions", "globals", "arrays", "structs") if _contracts else ("functions", "structs"):
            if kind != "structs" or not _compact:
                template = prefix_structs if kind == "structs" else _prefix[kind]
                result[kind] = {name: {**row, "provenance": provenance} for name, row in template.items()}
        result["unknown"] = [] if complete_layout else list(_prefix["unknown"])
        if _compact:
            result["structs"] = ProvenStructs(prefix_structs, provenance)
            result["authored_structs"] = []
    if authored_headers is not None:
        authored_names: set[str] = set()

        def authored(node: Any) -> bool:
            return bool(node.coord and node.coord.file and Path(node.coord.file).resolve() in authored_headers)

        class Homes(c_ast.NodeVisitor):  # type: ignore[misc]
            def visit_Struct(self, node: Any) -> None:
                if node.name and node.decls and authored(node):
                    authored_names.add(node.name)
                self.generic_visit(node)

            visit_Union = visit_Struct

            def visit_Typedef(self, node: Any) -> None:
                base = node.type.type if isinstance(node.type, c_ast.TypeDecl) else None
                if isinstance(base, (c_ast.Struct, c_ast.Union)) and not base.name and base.decls and authored(node):
                    authored_names.add(node.name)
                self.generic_visit(node)

        Homes().visit(tree)
        result["authored_structs"] = sorted(authored_names)
    for kind, name, row in declaration_rows(
        tree.ext, provenance, resolve, definitions=definitions, owned_source=owned_source, contracts=_contracts
    ):
        result[kind][name] = row
    if not complete_layout:
        rows, unknown = _layout_records(source, provenance, aliases)
        if _prefix is not None and (unknown or rows):
            raise _FullDeclarationUnit
        for name, row in rows.items():
            result["structs"][name] = row
        result["unknown"].extend(unknown)
    return result


def declaration_rows(
    nodes: list[Any],
    provenance: dict[str, Any],
    resolve: Callable[[str], str],
    *,
    definitions: bool,
    owned_source: Path | None,
    contracts: bool,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(kind, name, record) of each file-scope function, global and array declaration, in node order."""
    for node in nodes:
        definition = isinstance(node, c_ast.FuncDef)
        declaration = node.decl if definition else node
        if not isinstance(declaration, c_ast.Decl) or not declaration.name:
            continue
        if isinstance(declaration.type, c_ast.FuncDecl):
            if "static" in declaration.storage or (definitions and not definition and not contracts):
                continue
            params: list[dict[str, Any]] = []
            variadic = False
            arguments = declaration.type.args
            if arguments is not None:
                for param in arguments.params:
                    if isinstance(param, c_ast.EllipsisParam):
                        variadic = True
                    elif _type(param.type) != "void":
                        params.append({"name": param.name or f"arg{len(params)}", "type": _type(param.type)})
            yield (
                "functions",
                declaration.name,
                {
                    "return": _type(declaration.type.type),
                    "params": params,
                    "variadic": variadic,
                    "arity_known": arguments is not None,
                    "prototype": _prototype(declaration),
                    "registers": _registers(params, resolve),
                    "provenance": provenance,
                },
            )
        elif "static" not in declaration.storage and (
            contracts or not definitions or (owned_source is not None and declaration.coord.file == str(owned_source))
        ):
            type_, extern, array = _global(declaration)
            yield "globals", declaration.name, {"type": type_, "provenance": provenance, "declaration": extern}
            if array is not None:
                yield "arrays", declaration.name, {"type": array[0], "extent": array[1], "provenance": provenance}


def _layout_records(
    source: str, provenance: dict[str, Any], aliases: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    layout_source = _unit_clean("layouts", source, line_markers=False)
    rows, unknown = memo(
        "decl.layouts",
        (layout_source, tuple(aliases.items())),
        lambda: unit_layouts.records(layout_source, aliases),
        size=retention.memory_size,
        copy_out=retention.clone,
    )
    # Units whose rows are the same objects share one record dict, so its shared template is encoded once.
    identity = (json.dumps(provenance, sort_keys=True), tuple((name, id(row)) for name, row in rows.items()))
    records, _ = memo(
        "decl.records",
        identity,
        lambda: ({name: {**row, "provenance": provenance} for name, row in rows.items()}, rows),
        size=retention.memory_size,
        copy_out=retention.clone,
    )
    return records, list(unknown)


def _portable_signatures(seed: dict[str, Any], shared_aliases: dict[str, str]) -> list[str]:
    """A generated header cannot refer to a typedef private to one C source."""
    local = {name: type_ for name, type_ in seed["aliases"].items() if shared_aliases.get(name) != type_}
    if not local:
        return []
    return [prototype for name, record in seed["functions"].items() if (prototype := portable(name, record, local))]


def portable(name: str, record: dict[str, Any], local: dict[str, str]) -> str | None:
    """Spell RECORD's signature through LOCAL typedefs expanded (in place); its new prototype, or None if unchanged."""
    types = [record["return"], *(param["type"] for param in record["params"])]
    expanded = [canonical(type_, local) for type_ in types]
    if expanded == types:
        return None
    params = [declarator(type_, param["name"]) for type_, param in zip(expanded[1:], record["params"], strict=True)]
    if record["variadic"]:
        params.append("...")
    arguments = ", ".join(params) or ("void" if record["arity_known"] else "")
    storage_class = re.match(r"(?:(?:extern|static|inline)\s+)+", record["prototype"])
    prefix = "" if storage_class is None else storage_class[0]
    record["prototype"] = prefix + declarator(expanded[0], name + "(" + arguments + ")") + ";"
    # Width placeholders remain semantic unknowns even though their header
    # carrier uses the actual declared machine-width type.
    if not unknown(types[0]):
        record["return"] = expanded[0]
    record["params"] = [
        {**param, "type": type_ if unknown(param["type"]) else expanded_type}
        for param, type_, expanded_type in zip(record["params"], types[1:], expanded[1:], strict=True)
    ]
    return str(record["prototype"])


PREFIX_MEMO = 4


def _prefix_seed(prefix: str, contracts: bool) -> tuple[dict[str, Any], dict[str, bool], str]:
    """The header prefix's facts and typedef scope, parsed once per prefix text: units sharing their header
    expansion (the facts jobs group them) and a unit's two passes reuse it. Consumers treat it as read-only."""

    def parse() -> tuple[dict[str, Any], dict[str, bool], str]:
        cleaned = clean(prefix, line_markers=True)
        parser = cdecl.parser()
        seed = extract(
            cleaned,
            {},
            definitions=True,
            owned_source=Path("__unbake_header_prefix__"),
            _parser=parser,
            _contracts=contracts,
        )
        seed["shared_typedefs"] = seed["aliases"]
        seed["layout_source"] = cleaned
        return seed, parser._scope_stack[0].copy(), cleaned

    digest = hashlib.sha256(prefix.encode()).hexdigest()
    seed, scope, cleaned = memo(
        "decl.prefix", (digest, contracts), parse, size=retention.memory_size, copy_out=retention.clone
    )
    return seed, dict(scope), cleaned


def published(
    text: str | tuple[str, str],
    provenance: dict[str, Any],
    source: Path,
    *,
    contracts: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    """Facts of one preprocessed source: its header prefix seeds a scoped parse of its own unit."""
    if isinstance(text, tuple):
        prefix, suffix = text
    else:
        prefix, marker, suffix = text.partition(_BOUNDARY + "\n")
        if not marker:
            raise Held("solve", "types.declaration: missing preprocessor source boundary")
    seed, scope, cleaned = _prefix_seed(prefix, contracts)
    unit = _declaration_unit(_unit_clean("published", suffix, line_markers=True))
    # New aggregate definitions need the full layout context. Anonymous
    # extern declarations do not define named layouts and stay incremental.
    try:
        result = extract(
            unit,
            provenance,
            definitions=True,
            owned_source=source,
            _scope=scope,
            _prefix=seed,
            _compact=compact,
            _contracts=contracts,
        )
    except _FullDeclarationUnit:
        result = extract(cleaned + unit, provenance, definitions=True, owned_source=source, _contracts=contracts)
    if result["shared_typedefs"] is not seed["aliases"]:
        result["shared_typedefs"] = {**seed["aliases"], **result["shared_typedefs"]}
    prototypes = _portable_signatures(result, seed["aliases"])
    if prototypes:
        try:
            cdecl.parse("\n".join(prototypes), typedefs=scope)
        except Exception as error:
            raise Held("solve", f"types.declaration: {provenance}: emitted prototype: {error}") from error
    return result


def _receipt_seed(seed: dict[str, Any], provenance: dict[str, Any], *, compact: bool) -> dict[str, Any]:
    result = {**seed}
    for kind in ("functions", "globals", "arrays"):
        result[kind] = {name: {**row, "provenance": provenance} for name, row in seed[kind].items()}
    structs = seed["structs"]
    template = structs.template if isinstance(structs, ProvenStructs) else structs
    if compact:
        result["structs"] = ProvenStructs(template, provenance)
        result["authored_structs"] = []
    else:
        result["structs"] = {name: {**row, "provenance": provenance} for name, row in template.items()}
        result.pop("authored_structs", None)
    return result


def consumed_contracts(seed: dict[str, Any], text: str) -> dict[str, Any]:
    """An included header is not proof of its unused inferred declarations."""
    needed = set(re.findall(r"\b[A-Za-z_]\w*\b", declaration_source(text)))
    result = {**seed}
    for kind in ("functions", "globals", "arrays"):
        result[kind] = {name: row for name, row in seed[kind].items() if name in needed}
        for row in result[kind].values():
            needed.update(
                re.findall(
                    r"\b[A-Za-z_]\w*\b",
                    row.get("prototype", row.get("declaration", row.get("type", ""))),
                )
            )
    layouts = {}
    changed = True
    while changed:
        changed = False
        for name in list(needed):
            if name in seed["aliases"]:
                added = set(re.findall(r"\b[A-Za-z_]\w*\b", seed["aliases"][name])) - needed
                needed.update(added)
                changed |= bool(added)
        for name, row in seed["structs"].items():
            if name not in layouts and (name in needed or set(row.get("aliases", ())) & needed):
                layouts[name] = row
                needed.update(re.findall(r"\b[A-Za-z_]\w*\b", row["declaration"]))
                changed = True
    result["structs"] = layouts
    result["aliases"] = {name: row for name, row in seed["aliases"].items() if name in needed}
    result["shared_typedefs"] = {name: row for name, row in seed["shared_typedefs"].items() if name in needed}
    if "authored_structs" in seed:
        result["authored_structs"] = [name for name in seed["authored_structs"] if name in layouts]
    return result


def published_sources(project: Project) -> list[tuple[str, Path, str]]:
    """Use explicit published C ownership, never arbitrary scratch C files."""
    from unbake.layout import split as inventory

    return sorted(
        {
            (row.name, project.src / (row.path + ".c"), version)
            for version in project.versions
            for row in inventory.functions(project, version)
            if row.kind == "c"
        }
    )


def rooted(project: Project, text: str) -> str:
    """TEXT with the tree root's absolute path dropped from quoted paths (cpp line markers)."""
    return text.replace(f'"{project.root}/', '"').replace(f'"{project.root.resolve()}/', '"')


@pool.cpu
def _declared_job(job: tuple[Project, Host, str, dict[str, Any], set[Path]]) -> None:
    """Pool worker: one version's declared header facts, extracted into the shared store."""
    from unbake.typemap import facts

    project, policy, text, provenance, authored = job
    facts.store(project, policy).text(
        text, provenance, authored, lambda: extract(text, provenance, authored_headers=authored)
    )


@pool.cpu
def _version_text(job: tuple[Project, Host | None, str, dict[Path, str], Path | None, list[Path]]) -> str:
    """One version's preprocessed authored headers (and EXTRA after the generated ones): a pool task."""
    project, policy, version, contents, extra, ordered = job
    return _headers(project, policy, version, contents, extra, line_markers=True, ordered=ordered)


def _version_texts(
    project: Project, policy: Host | None, contents: dict[Path, str], extra: Path | None, ordered: list[Path]
) -> dict[str, str]:
    """Every version's preprocessed header text; each version's cpp runs in the worker pool."""
    jobs = [(project, policy, version, contents, extra, ordered) for version in project.versions]
    if policy is None:
        return {version: _version_text(job) for version, job in zip(project.versions, jobs, strict=True)}
    from unbake import pool

    return dict(zip(project.versions, pool.run(policy, _version_text, jobs), strict=True))


def collect(
    project: Project, policy: Host | None, keys: list[str], *, store: Any | None = None
) -> list[dict[str, Any]]:
    """Declared header seeds per version, declaration evidence, then every published source's facts. STORE is the
    one the caller keys the seeds with afterwards: it already knows the digest of every shared value they hold."""
    from unbake.typemap import facts

    project.build.mkdir(parents=True, exist_ok=True)
    return _collect(project, policy, facts.store(project, policy) if store is None else store, keys)


def _collect(project: Project, policy: Host | None, store: Any, keys: list[str]) -> list[dict[str, Any]]:
    from unbake.typemap import facts

    seeds = []
    declared: dict[str, dict[str, Any]] = {}
    with tui.task("Finding authored header inputs"):
        generated = storage.generated_view(project)
        contents = {
            path: path.read_text() for path, _ in include_headers(project, exclude=generated) if not generated(path)
        }
        ordered = ordered_headers(contents)
        authored = {path.resolve() for root in project.include for path in root.rglob("*.h") if not generated(path)}
    with tui.task("Preprocessing authored headers", len(project.versions)):
        texts = _version_texts(project, policy, contents, None, ordered)
    with tui.task("Reading authored header declarations", len(project.versions)):
        if policy is not None:
            # Each distinct version's declared headers are extracted in the worker pool into the shared store; the
            # loop below reads them back.
            from unbake import pool

            distinct = {
                text: {
                    "kind": "declared",
                    "version": version,
                    "sha256": inputs.bytes_digest(rooted(project, text).encode(), algorithm="sha256"),
                }
                for version, text in reversed(texts.items())
            }
            pool.run(policy, _declared_job, [(project, policy, text, row, authored) for text, row in distinct.items()])
        for version in project.versions:
            header_text = texts[version]
            # Line markers name absolute include paths: the digest reads them relative to the tree root, so the same
            # tree at another path records the same provenance.
            relative = rooted(project, header_text)
            provenance = {
                "kind": "declared",
                "version": version,
                "sha256": inputs.bytes_digest(relative.encode(), algorithm="sha256"),
            }
            seed = declared.get(header_text)
            if seed is None:
                seed = store.text(
                    header_text,
                    provenance,
                    authored,
                    lambda text=header_text, row=provenance: extract(text, row, authored_headers=authored),
                )
                declared[header_text] = seed
            else:
                seed = {**seed}
                for kind in ("functions", "globals", "structs", "arrays"):
                    seed[kind] = {name: {**row, "provenance": provenance} for name, row in seed[kind].items()}
            seeds.append(seed)
    from unbake.typemap import declaration_evidence

    with tui.task("Loading retained declaration evidence"):
        components = declaration_evidence.feedback_components(project)
        if components:
            exports = set().union(*(header_declarations(text).declared for text in components.values()))
            # Generated layouts are part of the prefix, while extern evidence is
            # retained at declared confidence (never promoted to an exact C proof).
            extra = project.build / "types" / "declaration_evidence.c"
            from unbake.layout import redeclarations
            from unbake.typemap import split
            from unbake.typemap.declaration_evidence import _body

            provided_layouts = {}
            for path in _generated_context(project):
                for statement in split.statements(_body(path.read_text())):
                    for name in re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", declaration_source(statement)):
                        provided_layouts[name] = statement
            supplemental = []
            for body in components.values():
                for statement in split.statements(body):
                    definitions = set(re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", declaration_source(statement)))
                    duplicates = definitions & provided_layouts.keys()
                    for name in duplicates:
                        if redeclarations.normalized(statement) != redeclarations.normalized(provided_layouts[name]):
                            raise Held(
                                "types",
                                f"declaration_evidence.{name}: local:\n{statement}\nshared:\n{provided_layouts[name]}",
                            )
                    if not duplicates:
                        supplemental.append(statement)
            extra.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(supplemental)
            if not extra.is_file() or extra.read_text() != content:
                atomic_files.text(extra, content)
            evidence: dict[str, dict[str, Any]] = {}
            evidence_texts = _version_texts(project, policy, contents, extra, ordered)

            def evidence_row(version: str) -> dict[str, Any]:
                return {
                    "kind": "declared",
                    "version": version,
                    "source": "declaration_evidence",
                    "sha256": inputs.bytes_digest(extra.read_bytes(), algorithm="sha256"),
                }

            if policy is not None:
                # Each distinct evidence text is extracted in the pool (under its first version's provenance, which the
                # loop below asks for), never one after another in this process.
                from unbake import pool

                first = {text: evidence_row(version) for version, text in reversed(evidence_texts.items())}
                pool.run(policy, _declared_job, [(project, policy, text, row, authored) for text, row in first.items()])
            for version in project.versions:
                provenance = evidence_row(version)
                # Evidence imports the generated context too. Preserve definition
                # homes so a canonical layout reused after a rename remains a
                # generated provider, with its split dependencies intact.
                text = evidence_texts[version]
                template = evidence.get(text)
                if template is None:
                    template = store.text(
                        text,
                        provenance,
                        authored,
                        lambda text=text, row=provenance: extract(text, row, authored_headers=authored),
                    )
                    evidence[text] = template
                seed = _receipt_seed(template, provenance, compact=False)
                if "authored_structs" in template:
                    seed["authored_structs"] = template["authored_structs"]
                for kind in ("functions", "globals", "arrays"):
                    seed[kind] = {name: row for name, row in seed[kind].items() if name in exports}
                # Explicit declaration evidence retains complete layouts even when
                # no mapped instruction currently uses them. Their output homes
                # are generated, but their confidence remains authored/declared.
                declared_layouts = {
                    name
                    for text in components.values()
                    for name in re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", declaration_source(text))
                }
                seed["authored_structs"] = sorted(set(seed["authored_structs"]) | declared_layouts)
                seeds.append(seed)
    # Published sources contribute their consumed contracts and owned definitions,
    # read from the facts cache and extracted only when their inputs changed.
    seeds.extend(facts.published(project, policy, store, keys))
    return seeds


def declarator(type_: str, name: str) -> str:
    """Insert a name in an abstract C type, including arrays and function pointers."""
    if "(*" in type_ or re.search(r"\(\s*\*", type_):
        return re.sub(r"(\(\s*\*[^)]*)(\))", rf"\g<1>{name}\2", type_, count=1)
    pointer_array = re.fullmatch(r"(.+?)\s*(\[[^\]]*\](?:\s*\[[^\]]*\])*)\s*(\*+)", type_)
    if pointer_array:
        base, dimensions, stars = pointer_array.groups()
        return f"{base.rstrip()} ({stars}{name}){dimensions}"
    array = type_.find("[")
    if array >= 0:
        return type_[:array].rstrip() + " " + name + type_[array:]
    return type_ + " " + name
