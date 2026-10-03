"""Explicit C declarations as target-ABI type seeds, without decompiler guesses."""

from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path
from typing import Any

from pycparser import c_ast, c_generator, c_parser  # type: ignore[import-untyped]

from unbake.decomp.draft_context import ordered_headers
from unbake.layout.structs_parser import Parser
from unbake.project.config import Held, Policy, Project
from unbake.typemap import storage


def clean(source: str, *, line_markers: bool = False) -> str:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", " ", source, flags=re.S)
    source = re.sub(r"^\s*#(?!\s*\d+\s+\")[^\n]*" if line_markers else r"^\s*#[^\n]*", "", source, flags=re.M)
    source = re.sub(r"\b(?:__extension__|__inline__|__inline|__restrict|restrict)\b", "", source)
    source = re.sub(r"\b__attribute__\s*\(\([^\n]*?\)\)", "", source)
    return source


def headers(
    project: Project, policy: Policy | None, version: str, extra: Path | None = None, *, line_markers: bool = False
) -> str:
    contents = {
        path: path.read_text()
        for root in project.include
        for path in sorted(root.rglob("*.h"))
        if not storage.generated(project, path)
    }
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
) -> str:
    ordered = ordered_headers(contents)
    if policy is None:
        # Raw guarded headers are useful to in-memory callers; other conditionals need cpp.
        for path, text in contents.items():
            if re.search(r"^\s*#\s*(?:if\b|elif\b|else\b)", text, re.M):
                raise Held("solve", f"types.declaration: {path}: policy.cpp required for conditional types")
        if extra is not None:
            if re.search(r"^\s*#\s*(?:if\b|ifdef\b|ifndef\b|elif\b|else\b)", extra.read_text(), re.M):
                raise Held("solve", f"types.declaration: {extra}: policy.cpp required for conditional C")
            return clean("\n".join(contents[path] for path in ordered) + "\n" + extra.read_text())
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
        source += f'#include "{extra}"\n'
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
    command = [
        str(policy.cpp),
        *(f"-I{root}" for root in project.include),
        *(flag for flag in policy.cppflags if not line_markers or flag != "-P"),
        *flags,
        *(("-P",) if extra is None and not line_markers else ()),
        "-x",
        "c",
        *(f"-D{macro}" for macro in project.version(version).macros),
        *(("-DUNBAKE_PROTOTYPES_H",) if extra is not None else ()),
        "-",
    ]
    try:
        result = subprocess.run(command, input=source, text=True, capture_output=True, cwd=project.root)
    except OSError as error:
        raise Held("solve", f"policy.cpp: {error}") from error
    if result.returncode:
        raise Held("solve", "types.declaration: " + result.stderr.strip())
    return clean(result.stdout, line_markers=extra is not None or line_markers)


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
) -> dict[str, Any]:
    source = clean(source, line_markers=owned_source is not None or authored_headers is not None)
    try:
        tree = c_parser.CParser().parse(source)
    except Exception as error:
        raise Held("solve", f"types.declaration: {provenance}: {error}") from error
    aliases = {node.name: _type(node.type) for node in tree.ext if isinstance(node, c_ast.Typedef)}
    result: dict[str, Any] = {
        "functions": {},
        "globals": {},
        "structs": {},
        "arrays": {},
        "aliases": aliases,
        "unknown": [],
    }
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
    try:
        layout_source = clean(source)
        for layout in Parser(layout_source).parse():
            if not layout.fields:
                continue
            result["structs"][layout.name] = {
                "type": f"{layout.kind} {layout.name}",
                "size": layout.size,
                "alignment": layout.alignment,
                "aliases": list(layout.aliases),
                "declaration": layout_source[layout.start : layout.end] + ";",
                "fields": [
                    {"name": f.name, "type": f.type, "offset": f.offset, "size": f.size, "extent": list(f.extent)}
                    for f in layout.fields
                ],
                "provenance": provenance,
            }
    except Held as error:
        result["unknown"].append("types.layout: " + error.reason)
    return result


def collect(project: Project, policy: Policy | None) -> list[dict[str, Any]]:
    seeds = []
    authored = {
        path.resolve() for root in project.include for path in root.rglob("*.h") if not storage.generated(project, path)
    }
    for version in project.versions:
        source = headers(project, policy, version, line_markers=True)
        seeds.append(
            extract(
                source,
                {"kind": "declared", "version": version, "sha256": storage.digest(source.encode())},
                authored_headers=authored,
            )
        )
    path = project.build / "types/proven.json"
    if path.is_file():
        records = storage.read(path, "types.feedback").get("records", {})
        for function, row in records.items():
            source = project.root / row["source"]
            for version in row["versions"]:
                text = headers(project, policy, version, source)
                seed = extract(
                    text,
                    {
                        "kind": "proven",
                        "function": function,
                        "version": version,
                        "source": row["source"],
                        "sha256": row["source_sha256"],
                        "proof": row["proof"],
                    },
                    definitions=True,
                    owned_source=source,
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
