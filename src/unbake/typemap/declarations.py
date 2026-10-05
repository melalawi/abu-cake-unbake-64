"""Explicit C declarations as target-ABI type seeds, without decompiler guesses."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import weakref
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import cdecl, prefixes
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
_C_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|^[ \t]*#[^\n]*|[A-Za-z_]\w*|\S', re.M)


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


def _declaration_unit(source: str) -> str:
    """_unit_bodies_blanked, once per text, resumed after the header prefix shared with recent units."""
    return memo(
        "decl.unit",
        source,
        lambda: prefixes.concatenated("unit.blank", source, _unit_bodies_blanked),
        keep=2 * UNIT_MEMO,
    )


def _unit_clean(stream: str, source: str, *, line_markers: bool) -> str:
    """clean() of a whole unit, once per text, resumed after the header prefix shared with recent units."""
    return memo(
        "decl.clean." + stream,
        (source, line_markers),
        lambda: prefixes.concatenated(
            f"unit.clean.{stream}.{line_markers}", source, lambda text: clean(text, line_markers=line_markers)
        ),
        keep=2 * UNIT_MEMO,
    )


def _unit_bodies_blanked(source: str) -> str:
    """Keep function definitions' signatures, never parse their implementation.

    Proven GCC code may contain label addresses, computed goto or inline asm.
    None contributes to declaration evidence. Blank bodies without moving line
    markers; aggregate definitions and file-scope initializers remain intact.
    """
    tokens = list(_C_TOKEN.finditer(source))
    edits = []
    depth = parens = 0
    assigned = False
    previous = ""
    index = 0
    while index < len(tokens):
        token = tokens[index][0]
        if token.startswith("#"):
            index += 1
            continue
        if token == "{" and depth == 0 and previous == ")" and not assigned:
            begin = tokens[index].end()
            level = 1
            index += 1
            while index < len(tokens) and level:
                level += (tokens[index][0] == "{") - (tokens[index][0] == "}")
                index += 1
            if level:
                raise Held("solve", "types.declaration: unclosed function body")
            edits.append((begin, tokens[index - 1].start()))
            assigned = False
            previous = "}"
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
        previous = token
        index += 1
    for begin, end in reversed(edits):
        body = source[begin:end]
        source = source[:begin] + re.sub(r"[^\n]", " ", body) + source[end:]
    return source


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
            keep=8,
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
    command = _cpp_command(project, policy, version, extra=extra is not None, line_markers=line_markers)
    text = _preprocess(project, command, source)
    return text if raw else clean(text, line_markers=extra is not None or line_markers)


def _cpp_command(project: Project, policy: Host, version: str, *, extra: bool, line_markers: bool) -> list[str]:
    flags: list[str] = []
    pending = iter(project.compilers[project.default_compiler].cflags)
    for flag in pending:
        if flag in ("-D", "-U", "-include", "-isystem"):
            value = next(pending, None)
            if value is None:
                raise Held("solve", f"compiler.cflags.{flag}: missing argument")
            flags.extend((flag, value))
        elif flag.startswith(("-D", "-U")):
            flags.append(flag)
    return [
        str(policy.cpp),
        *(f"-I{root}" for root in project.include),
        *(flag for flag in project.cppflags if not line_markers or flag != "-P"),
        *flags,
        *(("-P",) if not extra and not line_markers else ()),
        "-x",
        "c",
        *(f"-D{macro}" for macro in project.version(version).macros),
        *(("-DUNBAKE_PROTOTYPES_H",) if extra else ()),
        "-",
    ]


def _preprocess(project: Project, command: list[str], source: str) -> str:
    try:
        result = subprocess.run(command, input=source, text=True, capture_output=True, cwd=project.root)
    except OSError as error:
        raise Held("solve", f"policy.cpp: {error}") from error
    if result.returncode:
        raise Held("solve", "types.declaration: " + result.stderr.strip())
    return str(result.stdout)


def source_unit(project: Project, policy: Host | None, version: str, source: Path) -> str:
    """One source preprocessed with only its own includes, split by the source boundary."""
    return _headers(
        project, policy, version, {}, source, line_markers=False, ordered=[], raw=True, include_generated=False
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
    known: dict[str, str] = memo("decl.canonical", tuple(aliases.items()), dict, keep=4)

    def resolve(type_: str) -> str:
        found = known.get(type_)
        if found is None:
            found = known[type_] = canonical(type_, aliases)
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
    return memo("decl.tree", key, lambda: cdecl.resumable_parse(source, scope), keep=UNIT_MEMO)


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
    for node in tree.ext:
        definition = isinstance(node, c_ast.FuncDef)
        declaration = node.decl if definition else node
        if not isinstance(declaration, c_ast.Decl) or not declaration.name:
            continue
        if isinstance(declaration.type, c_ast.FuncDecl):
            if "static" in declaration.storage or (definitions and not definition and not _contracts):
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
            known_arity = arguments is not None
            result["functions"][declaration.name] = {
                "return": _type(declaration.type.type),
                "params": params,
                "variadic": variadic,
                "arity_known": known_arity,
                "prototype": _prototype(declaration),
                "registers": _registers(params, resolve),
                "provenance": provenance,
            }
        elif "static" not in declaration.storage and (
            _contracts or not definitions or (owned_source is not None and declaration.coord.file == str(owned_source))
        ):
            type_, extern, array = _global(declaration)
            result["globals"][declaration.name] = {"type": type_, "provenance": provenance, "declaration": extern}
            if array is not None:
                result["arrays"][declaration.name] = {"type": array[0], "extent": array[1], "provenance": provenance}
    if not complete_layout:
        rows, unknown = _layout_records(source, provenance, aliases)
        if _prefix is not None and (unknown or rows):
            raise _FullDeclarationUnit
        for name, row in rows.items():
            result["structs"][name] = row
        result["unknown"].extend(unknown)
    return result


def _layout_records(
    source: str, provenance: dict[str, Any], aliases: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    layout_source = _unit_clean("layouts", source, line_markers=False)
    rows, unknown = memo(
        "decl.layouts",
        (layout_source, tuple(aliases.items())),
        lambda: unit_layouts.records(layout_source, aliases),
        keep=UNIT_MEMO,
    )
    # Units whose rows are the same objects share one record dict, so its shared template is encoded once.
    identity = (json.dumps(provenance, sort_keys=True), tuple((name, id(row)) for name, row in rows.items()))
    records, _ = memo(
        "decl.records",
        identity,
        lambda: ({name: {**row, "provenance": provenance} for name, row in rows.items()}, rows),
        keep=8,
    )
    return records, list(unknown)


def _portable_signatures(seed: dict[str, Any], shared_aliases: dict[str, str]) -> list[str]:
    """A generated header cannot refer to a typedef private to one C source."""
    local = {name: type_ for name, type_ in seed["aliases"].items() if shared_aliases.get(name) != type_}
    rewritten: list[str] = []
    if not local:
        return rewritten
    for name, record in seed["functions"].items():
        types = [record["return"], *(param["type"] for param in record["params"])]
        expanded = [canonical(type_, local) for type_ in types]
        if expanded == types:
            continue
        params = [declarator(type_, param["name"]) for type_, param in zip(expanded[1:], record["params"], strict=True)]
        if record["variadic"]:
            params.append("...")
        arguments = ", ".join(params) or ("void" if record["arity_known"] else "")
        storage_class = re.match(r"(?:(?:extern|static|inline)\s+)+", record["prototype"])
        prefix = "" if storage_class is None else storage_class[0]
        record["prototype"] = prefix + declarator(expanded[0], name + "(" + arguments + ")") + ";"
        rewritten.append(record["prototype"])
        # Width placeholders remain semantic unknowns even though their header
        # carrier uses the actual declared machine-width type.
        if not unknown(types[0]):
            record["return"] = expanded[0]
        record["params"] = [
            {**param, "type": type_ if unknown(param["type"]) else expanded_type}
            for param, type_, expanded_type in zip(record["params"], types[1:], expanded[1:], strict=True)
        ]
    return rewritten


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
    seed, scope, cleaned = memo("decl.prefix", (digest, contracts), parse, keep=PREFIX_MEMO)
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


def _declared_job(job: tuple[Host, str, dict[str, Any], set[Path]]) -> None:
    """Pool worker: one version's declared header facts, extracted into the shared store."""
    from unbake.typemap import facts

    policy, text, provenance, authored = job
    facts.store(policy).text(text, provenance, authored, lambda: extract(text, provenance, authored_headers=authored))


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


def collect(project: Project, policy: Host | None, keys: list[str]) -> list[dict[str, Any]]:
    """Declared header seeds per version, declaration evidence, then every published source's facts."""
    from unbake.typemap import facts

    project.build.mkdir(parents=True, exist_ok=True)
    from unbake import journal

    temporary = journal.scratch(project.build, ".declarations-")
    try:
        return _collect(project, policy, temporary, facts.store(policy), keys)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _collect(project: Project, policy: Host | None, scratch: Path, store: Any, keys: list[str]) -> list[dict[str, Any]]:
    from unbake.typemap import facts

    seeds = []
    declared: dict[str, dict[str, Any]] = {}
    contents = {
        path: path.read_text()
        for path, _ in include_headers(project, exclude=lambda path: storage.generated(project, path))
        if not storage.generated(project, path)
    }
    ordered = ordered_headers(contents)
    authored = {
        path.resolve() for root in project.include for path in root.rglob("*.h") if not storage.generated(project, path)
    }
    texts = _version_texts(project, policy, contents, None, ordered)
    if policy is not None:
        # Each distinct version's declared headers are extracted in the worker pool into the shared store; the
        # loop below reads them back.
        from unbake import pool

        distinct = {
            text: {"kind": "declared", "version": version, "sha256": storage.digest(rooted(project, text).encode())}
            for version, text in reversed(texts.items())
        }
        pool.run(policy, _declared_job, [(policy, text, row, authored) for text, row in distinct.items()])
    for version in project.versions:
        header_text = texts[version]
        # Line markers name absolute include paths: the digest reads them relative to the tree root, so the same
        # tree at another path records the same provenance.
        relative = rooted(project, header_text)
        provenance = {"kind": "declared", "version": version, "sha256": storage.digest(relative.encode())}
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

    components = declaration_evidence.feedback_components(project)
    if components:
        exports = set().union(*(header_declarations(text).declared for text in components.values()))
        # Generated layouts are part of the prefix, while extern evidence is
        # retained at declared confidence (never promoted to an exact C proof).
        extra = scratch / "declaration_evidence.c"
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
        atomic_files.text(extra, "\n".join(supplemental))
        evidence: dict[str, dict[str, Any]] = {}
        evidence_texts = _version_texts(project, policy, contents, extra, ordered)
        for version in project.versions:
            provenance = {
                "kind": "declared",
                "version": version,
                "source": "declaration_evidence",
                "sha256": storage.digest(extra.read_bytes()),
            }
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
