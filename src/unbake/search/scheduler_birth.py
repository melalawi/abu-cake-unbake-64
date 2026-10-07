"""Explicit, source-mapped experiment for a printed GCC birth-priority pair."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from unbake import cdecl
from unbake.search.core import Context, Mutation
from unbake.search.loops import walk
from unbake.work.compare import Compared

name = "scheduler-birth"
needs_compiler_facts = True


def _integer(decl: Any, tree: Any, ast: Any) -> bool:
    """Require a proven 32-bit integer spelling, including locally defined aliases."""
    if not isinstance(decl.type, ast.TypeDecl) or decl.quals or decl.storage:
        return False
    node = decl.type.type
    aliases = {n.name: n for n in tree.ext if isinstance(n, ast.Typedef)}
    seen = set()
    while isinstance(node, ast.IdentifierType) and len(node.names) == 1 and node.names[0] in aliases:
        ident = node.names[0]
        if ident in seen:
            return False
        seen.add(ident)
        alias = aliases[ident]
        if not isinstance(alias.type, ast.TypeDecl) or any(getattr(n, "quals", []) for n in walk(alias)):
            return False
        node = alias.type.type
    return (
        isinstance(node, ast.IdentifierType)
        and set(node.names) <= {"int", "signed", "unsigned", "long"}
        and bool(node.names)
    )


def _expression(node: Any, bindings: dict[str, Any], tree: Any, ast: Any) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.type == "int")
    if isinstance(node, ast.ID):
        return node.name in bindings and _integer(bindings[node.name], tree, ast)
    if isinstance(node, ast.BinaryOp) and node.op in ("+", "-", "*", "<<", ">>"):
        return _expression(node.left, bindings, tree, ast) and _expression(node.right, bindings, tree, ast)
    if isinstance(node, ast.UnaryOp) and node.op == "*" and isinstance(node.expr, ast.ID):
        pointer = bindings.get(node.expr.name)
        if (
            pointer is not None
            and isinstance(pointer.type, ast.PtrDecl)
            and not pointer.quals
            and not pointer.type.quals
        ):
            pointee = deepcopy(pointer)
            pointee.type = pointee.type.type
            return _integer(pointee, tree, ast)
    return False


def propose(source: str, trial: Compared, ctx: Context) -> Iterator[Mutation]:
    from pycparser import c_ast as ast  # type: ignore[import-untyped]
    from pycparser import c_generator

    evidence = ctx.compiler_facts or {}
    options = [
        option for r in evidence.get("regions", []) for option in r.get("compiler_facts", {}).get("search_options", [])
    ]
    if len(options) != 1 or options[0]["method"] != name:
        return
    option = options[0]
    cleaned = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
        lambda m: m[0] if m[0].startswith('"') else re.sub(r"[^\n]", " ", m[0]),
        source,
        flags=re.S,
    )
    if re.search(r"^\s*#", cleaned, re.M):
        return
    try:
        tree = cdecl.parse(cleaned)
    except (cdecl.ParseError, AssertionError):
        return
    functions = [n for n in tree.ext if isinstance(n, ast.FuncDef) and n.decl.name == trial.function]
    if len(functions) != 1:
        return
    function = functions[0]
    declarations = [n for n in walk(function) if isinstance(n, ast.Decl)]
    counts = Counter(n.name for n in declarations)
    if any(count > 1 for count in counts.values()) or any(
        isinstance(n, (ast.Goto, ast.Label, ast.For, ast.While, ast.DoWhile)) or "volatile" in getattr(n, "quals", [])
        for n in walk(function)
    ):
        return
    source_lines = source.splitlines()
    update_lines = [i + 1 for i, line in enumerate(source_lines) if line.strip() == option["update_expression"]]
    birth_lines = [i + 1 for i, line in enumerate(source_lines) if line.strip() == option["birth_expression"]]
    if len(update_lines) != 1 or len(birth_lines) != 1:
        return
    printer = c_generator.CGenerator()
    for block in walk(function.body):
        if not isinstance(block, ast.Compound):
            continue
        items = block.block_items or []
        for index in range(1, len(items) - 1):
            birth, update, consumer = items[index - 1 : index + 2]
            if not (
                isinstance(update, ast.Assignment)
                and update.op == "+="
                and isinstance(update.lvalue, ast.ID)
                and update.coord.line == update_lines[0]
                and isinstance(update.rvalue, ast.Constant)
                and update.rvalue.type == "int"
                and update.rvalue.value == "2"
                and isinstance(birth, ast.Decl)
                and birth.coord.line == birth_lines[0]
                and _integer(birth, tree, ast)
                and birth.init is not None
                and isinstance(consumer, ast.Assignment)
                and consumer.op == "="
                and isinstance(consumer.rvalue, ast.BinaryOp)
                and consumer.rvalue.op == "-"
                and isinstance(consumer.rvalue.left, ast.ID)
                and consumer.rvalue.left.name == birth.name
                and isinstance(consumer.rvalue.right, ast.ID)
                and consumer.rvalue.right.name == update.lvalue.name
            ):
                continue
            variable = update.lvalue.name
            binding = [n for n in declarations if n.name == variable]
            if len(binding) != 1 or not _integer(binding[0], tree, ast):
                continue
            if printer.visit(binding[0].type.type) != printer.visit(birth.type.type):
                continue
            if not _expression(birth.init, {n.name: n for n in declarations}, tree, ast):
                continue
            if not (
                isinstance(consumer.lvalue, ast.ID)
                or (
                    isinstance(consumer.lvalue, ast.UnaryOp)
                    and consumer.lvalue.op == "*"
                    and isinstance(consumer.lvalue.expr, ast.ID)
                )
            ):
                continue
            if any(
                isinstance(n, (ast.FuncCall, ast.Assignment, ast.Cast, ast.TernaryOp))
                or (isinstance(n, ast.UnaryOp) and n.op in ("p++", "p--", "++", "--"))
                or (isinstance(n, ast.Constant) and n.type in ("float", "double"))
                for n in walk(birth.init)
            ):
                continue
            if any(
                isinstance(n, ast.UnaryOp) and n.op == "&" and isinstance(n.expr, ast.ID) and n.expr.name == variable
                for n in walk(function)
            ):
                continue
            nodes = list(walk(function))
            after = next(i for i, n in enumerate(nodes) if n is consumer) + len(list(walk(consumer)))
            if any(isinstance(n, ast.ID) and n.name == variable for n in nodes[after:]):
                continue
            if sum(isinstance(n, ast.ID) and n.name == variable for n in walk(consumer)) != 1:
                continue
            taken = {n.name for n in walk(tree) if isinstance(n, (ast.ID, ast.Decl, ast.Typedef))}
            fresh = "schedule_value"
            suffix = 0
            while fresh in taken:
                suffix += 1
                fresh = f"schedule_value_{suffix}"
            new_decl = deepcopy(binding[0])
            new_decl.name, new_decl.type.declname = fresh, fresh
            new_decl.init, new_decl.storage = None, []
            replacement = deepcopy(block)
            new_items = replacement.block_items
            new_items[index] = ast.Assignment(
                "=", ast.ID(fresh), ast.BinaryOp("+", ast.ID(variable), deepcopy(update.rvalue))
            )
            new_items[index + 1].rvalue.right = ast.ID(fresh)
            new_items.insert(0, new_decl)
            made = deepcopy(tree)
            position = next(i for i, n in enumerate(walk(tree)) if n is block)
            cloned = next(n for i, n in enumerate(walk(made)) if i == position)
            cloned.block_items = replacement.block_items
            comments = re.findall(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', source, re.S)
            comments = [c for c in comments if c.startswith("/")]
            text = "".join(c + "\n" for c in comments) + printer.visit(made) + "\n"
            yield Mutation(
                name,
                f"materialize mapped integer update uid {option['update_uid']} "
                f"competing with birth uid {option['birth_uid']}",
                text,
            )
            return
