"""Explicit C declarations as target-ABI type seeds, without decompiler guesses."""

from __future__ import annotations

import copy
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator, c_parser  # type: ignore[import-untyped]

from unbake.decomp.draft_context import ordered_headers
from unbake.decomp.header_declarations import declarations as header_declarations
from unbake.layout.structs_parser import Parser
from unbake.project.config import Held, Policy, Project
from unbake.project.headers import include_headers
from unbake.typemap import storage

_BOUNDARY = "extern int __unbake_feedback_boundary;"
_C_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|^[ \t]*#[^\n]*|[A-Za-z_]\w*|\S', re.M)


def _declaration_unit(source: str) -> str:
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
    # Attributes can contain calls and strings: a non-greedy regex leaves the
    # last ')' of section(".sdata") behind in otherwise valid declarations.
    while match := re.search(r"\b__attribute__\s*\(", source):
        level = 1
        end = match.end()
        for token in _C_TOKEN.finditer(source, end):
            level += (token[0] == "(") - (token[0] == ")")
            end = token.end()
            if not level:
                break
        if level:
            raise Held("solve", "types.declaration: unclosed attribute")
        source = source[: match.start()] + re.sub(r"[^\n]", " ", source[match.start() : end]) + source[end:]
    return source


def headers(
    project: Project, policy: Policy | None, version: str, extra: Path | None = None, *, line_markers: bool = False
) -> str:
    contents = {path: path.read_text() for path, _ in include_headers(project) if not storage.generated(project, path)}
    if extra is None:
        from unbake.project.cache import remembered

        selection = (
            project.root,
            version,
            line_markers,
            None if policy is None else (str(policy.cpp), tuple(policy.cppflags)),
            project.compilers[project.default_compiler].cflags,
            project.include,
            project.version(version).macros,
            tuple(sorted(contents.items())),
        )
        return remembered(
            "typemap.headers",
            selection,
            lambda: _headers(project, policy, version, contents, None, line_markers=line_markers),
            keep=8,
        )
    return _headers(project, policy, version, contents, extra, line_markers=line_markers)


def _headers(
    project: Project,
    policy: Policy | None,
    version: str,
    contents: dict[Path, str],
    extra: Path | None,
    *,
    line_markers: bool,
    ordered: list[Path] | None = None,
    raw: bool = False,
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
        generated_types = project.include[0] / "shared/typemap.h"
        if generated_types.is_file():
            source += f'#include "{generated_types}"\n'
        if raw:
            source += _BOUNDARY + "\n"
        source += f'#include "{extra}"\n'
    command = _cpp_command(project, policy, version, extra=extra is not None, line_markers=line_markers)
    text = _preprocess(project, command, source)
    return text if raw else clean(text, line_markers=extra is not None or line_markers)


def _cpp_command(project: Project, policy: Policy, version: str, *, extra: bool, line_markers: bool) -> list[str]:
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
        *(flag for flag in policy.cppflags if not line_markers or flag != "-P"),
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


class _PublishedHeaders:
    """Preprocess the common includes once, then replay their final macro state.

    Each source still runs cpp independently, so its defines and undefines cannot
    affect another source. Include guards suppress already-emitted declarations.
    Stateful preprocessor extensions retain the ordinary full-unit path.
    """

    def __init__(self, project: Project, policy: Policy | None, scratch: Path) -> None:
        self.project, self.policy, self.scratch = project, policy, scratch
        self.contents = {
            path: path.read_text() for path, _ in include_headers(project) if not storage.generated(project, path)
        }
        self.ordered = ordered_headers(self.contents)
        texts = list(self.contents.values())
        generated = project.include[0] / "shared/typemap.h" if project.include else None
        if generated is not None and generated.is_file():
            texts.append(generated.read_text())
        self.replay = policy is not None and not any(
            re.search(r"__COUNTER__|^\s*#\s*pragma\b", text, re.M) for text in texts
        )
        self.guarded = {}
        consumed = dict(self.contents)
        if generated is not None and generated.is_file():
            consumed[generated] = generated.read_text()
        for path, text in consumed.items():
            text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
            guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", text)
            if guard and re.search(r"#\s*endif[^\n]*\s*\Z", text):
                # Only suppress files enclosed by that one outer guard.
                depth = 0
                complete = True
                directives = list(re.finditer(r"^\s*#\s*(if|ifdef|ifndef|endif|else|elif)\b", text, re.M))
                for index, directive in enumerate(directives):
                    if directive[1] in ("else", "elif"):
                        if depth == 1:
                            complete = False
                    else:
                        depth += 1 if directive[1] != "endif" else -1
                    if depth == 0 and index != len(directives) - 1:
                        complete = False
                if complete and depth == 0:
                    self.guarded[path.resolve()] = guard[1]
        self.prepared: dict[str, tuple[str, list[str]] | None] = {}

    def source(self, version: str, source: Path) -> str | tuple[str, str]:
        project, policy = self.project, self.policy
        if self.replay and version not in self.prepared:
            assert policy is not None
            raw = _headers(
                project, policy, version, self.contents, source, line_markers=False, ordered=self.ordered, raw=True
            )
            prefix, marker, _ = raw.partition(_BOUNDARY + "\n")
            if not marker:
                raise Held("solve", "types.declaration: missing preprocessor source boundary")
            prelude = "".join(f'#include "{path}"\n' for path in self.ordered)
            generated = project.include[0] / "shared/typemap.h" if project.include else None
            if generated is not None and generated.is_file():
                prelude += f'#include "{generated}"\n'
            command = _cpp_command(project, policy, version, extra=True, line_markers=False)
            macros = _preprocess(project, [*command[:-1], "-dM", "-"], prelude)
            # A provider must support cpp's macro dump, including fixture providers.
            if not macros.startswith("#define "):
                self.prepared[version] = None
            else:
                path = self.scratch / (version + ".macros.h")
                path.write_text(macros)
                # Forced includes were already consumed while preparing the prefix.
                pending = iter(command)
                replay = []
                for flag in pending:
                    if flag == "-include":
                        next(pending)
                    else:
                        replay.append(flag)
                self.prepared[version] = prefix, [*replay[:-1], "-imacros", str(path), "-"]
        prepared = self.prepared.get(version)
        if prepared is None:
            return _headers(
                project, policy, version, self.contents, source, line_markers=False, ordered=self.ordered, raw=True
            )
        prefix, command = prepared
        content = source.read_text()
        temporary = None
        if not re.search(r"^\s*#\s*(?:undef|define|include_next)\b", content, re.M):
            # A consumed, fully guarded include has no remaining effects. Avoid
            # opening megabytes of those headers just to rediscover their guards.
            def include(match: re.Match[str]) -> str:
                relative = match[1]
                paths = (source.parent / relative, *(root / relative for root in project.include))
                found = next((path.resolve() for path in paths if path.is_file()), None)
                if found in self.guarded:
                    return "\n" * match[0].count("\n")
                return match[0]

            content = re.sub(r'^\s*#\s*include\s*"([^"\n]+)"[^\n]*(?:\n|$)', include, content, flags=re.M)
            with tempfile.NamedTemporaryFile(
                mode="w", prefix=version + ".", suffix=".c", dir=self.scratch, delete=False
            ) as stream:
                stream.write(f'#line 1 "{source}"\n' + content)
                temporary = Path(stream.name)
            command = [*command[:-1], "-iquote", str(source.parent), "-"]
            source = temporary
        try:
            suffix = _preprocess(project, command, f'#include "{source}"\n')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return prefix, suffix


def _source_units(
    headers_batch: _PublishedHeaders, tasks: list[tuple[str, Path, str, dict[str, Any]]]
) -> Iterator[tuple[tuple[str, Path, str, dict[str, Any]], str | tuple[str, str] | Held]]:
    """Bound cpp concurrency and preserve receipt order for deterministic merging."""
    dummy = headers_batch.scratch / ".empty.c"
    dummy.write_text("")
    for version in dict.fromkeys(task[2] for task in tasks):
        headers_batch.source(version, dummy)

    def preprocess(task: tuple[str, Path, str, dict[str, Any]]) -> str | tuple[str, str] | Held:
        try:
            return headers_batch.source(task[2], task[1])
        except Held as error:
            return error

    cores = min(8, getattr(headers_batch.policy, "cores", 1))
    with ThreadPoolExecutor(max_workers=cores) as pool:
        for start in range(0, len(tasks), 64):
            chunk = tasks[start : start + 64]
            yield from zip(chunk, pool.map(preprocess, chunk), strict=True)


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


class _SeededParser(c_parser.CParser):  # type: ignore[misc]
    """Resume the file scope of an exact preprocessed prefix (pinned pycparser 3)."""

    def __init__(self, scope: dict[str, bool]) -> None:
        super().__init__()
        self.scope = scope

    def _parse_translation_unit_or_empty(self) -> Any:
        self._scope_stack = [self.scope.copy()]
        return super()._parse_translation_unit_or_empty()


class _FullDeclarationUnit(Exception):
    """A source changes the layout meaning of its shared header prefix."""


def _type(node: Any) -> str:
    node = copy.deepcopy(node)

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
        "unsigned": "unsigned int",
        "short int": "short",
        "long int": "long",
        "unsigned long int": "unsigned long",
    }.get(type_, type_)


def unknown(type_: str) -> bool:
    """Decompiler placeholders specify machine widths, not semantic C types."""
    return bool(re.search(r"\bM2C_(?:UNK|UNKNOWN)\w*\b", type_))


def parameter_registers(params: list[dict[str, Any]], aliases: dict[str, str]) -> list[str | None]:
    """O32 first four words, including the leading floating-point register rule."""
    result: list[str | None] = []
    slot = 0
    floating_prefix = True
    for param in params:
        type_ = canonical(param["type"], aliases)
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
) -> dict[str, Any]:
    source = clean(source, line_markers=owned_source is not None or authored_headers is not None)
    source = _declaration_unit(source)
    try:
        parser = _parser or (c_parser.CParser() if _scope is None else _SeededParser(_scope))
        tree = parser.parse(source)
    except Exception as error:
        raise Held("solve", f"types.declaration: {provenance}: {error}") from error
    incoming = {node.name: _type(node.type) for node in tree.ext if isinstance(node, c_ast.Typedef)}
    aliases = {} if _prefix is None else _prefix["aliases"] if _compact and not incoming else dict(_prefix["aliases"])
    aliases.update(incoming)
    prefix_structs = {} if _prefix is None else _prefix["structs"]
    complete_layout = False
    if _prefix is not None:
        additions = []
        overrides = bool(incoming and _prefix["unknown"])
        for name, type_ in incoming.items():
            if name in _prefix["aliases"]:
                if type_ != _prefix["aliases"][name]:
                    overrides = True
                continue
            target = canonical(type_, aliases)
            aggregate = re.fullmatch(r"(?:struct|union) (\w+)", target)
            if aggregate is not None:
                additions.append((aggregate[1], name))
            elif target.startswith(("struct {", "union {", "enum ")):
                overrides = True
        if overrides:
            variants = _prefix.setdefault("layout_overrides", {})
            selection = tuple(incoming.items())
            if selection not in variants:
                rows, unknown = _layout_records(_prefix["layout_source"] + source, {}, aliases)
                if unknown:
                    raise _FullDeclarationUnit
                variants[selection] = rows
            prefix_structs = variants[selection]
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
        "unknown": [],
    }
    if _prefix is not None:
        for kind in ("functions", "structs"):
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
    generator = c_generator.CGenerator()
    for node in tree.ext:
        definition = isinstance(node, c_ast.FuncDef)
        declaration = node.decl if definition else node
        if not isinstance(declaration, c_ast.Decl) or not declaration.name:
            continue
        if isinstance(declaration.type, c_ast.FuncDecl):
            if definitions and not definition:
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
                "prototype": generator.visit(declaration) + ";",
                "registers": parameter_registers(params, aliases),
                "provenance": provenance,
            }
        elif "static" not in declaration.storage and (
            not definitions or (owned_source is not None and declaration.coord.file == str(owned_source))
        ):
            type_ = _type(declaration.type)
            declaration = copy.deepcopy(declaration)
            declaration.init = None
            result["globals"][declaration.name] = {
                "type": type_,
                "provenance": provenance,
                "declaration": "extern " + generator.visit(declaration).removeprefix("extern ") + ";",
            }
            if isinstance(declaration.type, c_ast.ArrayDecl):
                result["arrays"][declaration.name] = {
                    "type": _type(declaration.type.type),
                    "extent": generator.visit(declaration.type.dim) if declaration.type.dim is not None else None,
                    "provenance": provenance,
                }
    if not complete_layout:
        rows, unknown = _layout_records(source, provenance, aliases)
        if _prefix is not None and (unknown or rows):
            raise _FullDeclarationUnit
        for name, row in rows.items():
            result["structs"][name] = row
        result["unknown"].extend(unknown)
    return result


def _layout_typedefs(declaration: str, aliases: dict[str, str]) -> dict[str, str]:
    """Keep the typedef prerequisites of a retained aggregate's C spelling."""
    # Resolve prerequisite spellings so a pointer callback can precede the
    # aggregate whose fields use it, without introducing a typedef cycle.
    return {
        name: canonical(aliases[name], aliases)
        for name in sorted(header_declarations(declaration).uses)
        if name in aliases
    }


def _layout_records(
    source: str, provenance: dict[str, Any], aliases: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    records: dict[str, Any] = {}
    unknown = []
    layout_source = clean(source)
    try:
        for layout in Parser(layout_source).parse():
            if not layout.fields:
                continue
            declaration = layout_source[layout.start : layout.end] + ";"
            records[layout.name] = {
                "type": f"{layout.kind} {layout.name}",
                "size": layout.size,
                "alignment": layout.alignment,
                "aliases": list(layout.aliases),
                "declaration": declaration,
                "typedefs": _layout_typedefs(declaration, aliases),
                "fields": [
                    {"name": f.name, "type": f.type, "offset": f.offset, "size": f.size, "extent": list(f.extent)}
                    for f in layout.fields
                ],
                "provenance": provenance,
            }
    except Held as error:
        unknown.append("types.layout: " + error.reason)
    return records, unknown


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


class _PublishedDeclarations:
    """Share header parsing without sharing a source's preprocessor or C scope."""

    def __init__(self) -> None:
        self.prefixes: dict[str, tuple[str, dict[str, bool], dict[str, Any]]] = {}
        self.prefix_errors: dict[str, tuple[str, str]] = {}
        self.sources: dict[tuple[str, str], dict[str, Any]] = {}

    def extract(
        self, text: str | tuple[str, str], provenance: dict[str, Any], source: Path, *, compact: bool = False
    ) -> dict[str, Any]:
        if isinstance(text, tuple):
            prefix, suffix = text
        else:
            prefix, marker, suffix = text.partition(_BOUNDARY + "\n")
            if not marker:
                raise Held("solve", "types.declaration: missing preprocessor source boundary")
        if prefix in self.prefix_errors:
            phase, reason = self.prefix_errors[prefix]
            raise Held(phase, reason)
        cached = self.prefixes.get(prefix)
        if cached is None:
            cleaned = clean(prefix, line_markers=True)
            parser = c_parser.CParser()
            try:
                seed = extract(
                    cleaned, {}, definitions=True, owned_source=Path("__unbake_header_prefix__"), _parser=parser
                )
            except Held as error:
                # The exact prefix fails independently of every source suffix.
                # Preflight must retain each source's verdict without parsing
                # the same broken headers hundreds of times. Store only text,
                # never tracebacks that retain a partially parsed header AST.
                self.prefix_errors[prefix] = error.phase, error.reason
                raise
            seed["layout_source"] = cleaned
            cached = cleaned, parser._scope_stack[0].copy(), seed
            self.prefixes[prefix] = cached
        cleaned, scope, seed = cached
        suffix = _declaration_unit(clean(suffix, line_markers=True))
        source_key = None
        if not re.search(r'^\s*#\s*\d+\s+"', suffix, re.M):
            # Without line markers, the existing ownership rule excludes source
            # globals. Resident literal storage and function bodies therefore
            # cannot change these declaration facts across containing versions.
            interface = re.sub(
                r"\bconst\s+(?:float|double|unsigned\s+(?:int|char|short)|int|char|short|long)\s+"
                r"unbake_rodata_[0-9A-Fa-f]+_[0-9A-Fa-f]+[^;]*;",
                "",
                suffix,
            )
            source_key = prefix, re.sub(r"\{\s*\}", "{}", interface).rstrip()
            previous = self.sources.get(source_key)
            if previous is not None:
                return _receipt_seed(previous, provenance, compact=compact)
        # New aggregate definitions need the full layout context. Anonymous
        # extern declarations do not define named layouts and stay incremental.
        try:
            result = extract(
                suffix, provenance, definitions=True, owned_source=source, _scope=scope, _prefix=seed, _compact=compact
            )
        except _FullDeclarationUnit:
            result = extract(cleaned + suffix, provenance, definitions=True, owned_source=source)
        prototypes = _portable_signatures(result, seed["aliases"])
        if prototypes:
            try:
                _SeededParser(scope).parse("\n".join(prototypes))
            except Exception as error:
                raise Held("solve", f"types.declaration: {provenance}: emitted prototype: {error}") from error
        if (
            source_key is not None
            and not result["globals"]
            and not result["arrays"]
            and result["unknown"] == seed["unknown"]
        ):
            self.sources[source_key] = result
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


def collect(project: Project, policy: Policy | None) -> list[dict[str, Any]]:
    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".declarations-", dir=project.build) as temporary:
        return _collect(project, policy, Path(temporary))


def validate_sources(
    project: Project, policy: Policy | None, entries: list[tuple[str, Path, tuple[str, ...]]]
) -> dict[str, str]:
    """Refuse declaration errors in the staged batch before proof/publication."""
    if not entries:
        return {}
    project.build.mkdir(parents=True, exist_ok=True)
    refused = {}
    with tempfile.TemporaryDirectory(prefix=".declarations-", dir=project.build) as temporary:
        headers_batch = _PublishedHeaders(project, policy, Path(temporary))
        published = _PublishedDeclarations()
        tasks: list[tuple[str, Path, str, dict[str, Any]]] = [
            (function, source, version, {}) for function, source, versions in entries for version in versions
        ]
        for (function, source, version, _), text in _source_units(headers_batch, tasks):
            if function in refused:
                continue
            try:
                if isinstance(text, Held):
                    raise text
                published.extract(
                    text, {"kind": "proven", "function": function, "version": version}, source, compact=True
                )
            except Held as error:
                refused[function] = error.reason
    return refused


def _collect(project: Project, policy: Policy | None, scratch: Path) -> list[dict[str, Any]]:
    seeds = []
    declared: dict[str, dict[str, Any]] = {}
    headers_batch = _PublishedHeaders(project, policy, scratch)
    published = _PublishedDeclarations()
    authored = {
        path.resolve() for root in project.include for path in root.rglob("*.h") if not storage.generated(project, path)
    }
    for version in project.versions:
        header_text = _headers(
            project, policy, version, headers_batch.contents, None, line_markers=True, ordered=headers_batch.ordered
        )
        provenance = {"kind": "declared", "version": version, "sha256": storage.digest(header_text.encode())}
        seed = declared.get(header_text)
        if seed is None:
            seed = extract(header_text, provenance, authored_headers=authored)
            declared[header_text] = seed
        else:
            seed = {**seed}
            for kind in ("functions", "globals", "structs", "arrays"):
                seed[kind] = {name: {**row, "provenance": provenance} for name, row in seed[kind].items()}
        seeds.append(seed)
    path = project.build / "types/proven.json"
    if path.is_file():
        records = storage.read(path, "types.feedback").get("records", {})
        tasks = [
            (function, project.root / row["source"], version, row)
            for function, row in records.items()
            for version in row["versions"]
        ]
        for (function, source, version, row), text in _source_units(headers_batch, tasks):
            if isinstance(text, Held):
                raise text
            seed = published.extract(
                text,
                {
                    "kind": "proven",
                    "function": function,
                    "version": version,
                    "source": row["source"],
                    "sha256": row["source_sha256"],
                    "proof": row["proof"],
                },
                source,
                compact=True,
            )
            seed["functions"] = {name: value for name, value in seed["functions"].items() if name == function}
            seeds.append(seed)
    return seeds


def declarator(type_: str, name: str) -> str:
    """Insert a name in an abstract C type, including arrays and function pointers."""
    if "(*" in type_ or re.search(r"\(\s*\*", type_):
        return re.sub(r"(\(\s*\*[^)]*)(\))", rf"\g<1>{name}\2", type_, count=1)
    array = type_.find("[")
    if array >= 0:
        return type_[:array].rstrip() + " " + name + type_[array:]
    return type_ + " " + name
