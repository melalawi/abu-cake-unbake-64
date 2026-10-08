"""Prioritized lexical numeric lifetimes and terminal tail strategies."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from unbake import cdecl
from unbake.search.core import Context, Mutation
from unbake.search.loops import walk
from unbake.search.order import _replace, _Safety, _terminal_tails
from unbake.search.validate import check_dominance
from unbake.work.compare import Compared

name = "creative"
needs_preprocess = True


def _conversions(function: Any, ast: Any, safety: _Safety) -> Iterator[tuple[Any, Any]]:
    declarations = [node for node in walk(function) if isinstance(node, ast.Decl)]
    for block in walk(function.body):
        if not isinstance(block, ast.Compound):
            continue
        items = block.block_items or []
        for start in range(len(items) - 2):
            define, store, convert = items[start : start + 3]
            if not isinstance(define, ast.Assignment) or not isinstance(define.lvalue, ast.ID):
                continue
            old = define.lvalue.name
            decl = safety.types.get(old)
            if (
                decl is None
                or sum(node.name == old for node in declarations) != 1
                or old in safety.escaped
                or old in safety.volatile
            ):
                continue
            if not isinstance(decl.type, ast.TypeDecl) or not isinstance(decl.type.type, ast.IdentifierType):
                continue
            if (
                not isinstance(store, ast.Assignment)
                or store.op != "="
                or not isinstance(store.rvalue, ast.ID)
                or store.rvalue.name != old
            ):
                continue
            if not isinstance(convert, ast.Assignment) or convert.op != "=" or not isinstance(convert.rvalue, ast.Cast):
                continue
            if not isinstance(convert.rvalue.expr, ast.ID) or convert.rvalue.expr.name != old:
                continue
            if not check_dominance(block, old, start, start + 3, ast):
                continue
            # A later read before another definition would observe the old live
            # value; preserve it by refusing this narrower rewrite.
            later_read = False
            for item in items[start + 3 :]:
                if (
                    isinstance(item, ast.Assignment)
                    and item.op == "="
                    and isinstance(item.lvalue, ast.ID)
                    and item.lvalue.name == old
                ):
                    break
                if any(isinstance(node, ast.ID) and node.name == old for node in walk(item)):
                    later_read = True
                    break
            # The old local cannot be observed outside a terminal straight-line
            # arm. Earlier definitions in other arms are independent lifetimes.
            if not items or not isinstance(items[-1], ast.Return):
                continue
            if later_read or any(
                isinstance(node, (ast.Label, ast.Goto)) for item in items[start + 3 :] for node in walk(item)
            ):
                continue
            result_name = convert.lvalue.name if isinstance(convert.lvalue, ast.ID) else None
            if (
                result_name is None
                or sum(node.name == result_name for node in declarations) != 1
                or result_name in safety.escaped
                or result_name in safety.volatile
            ) or any(
                isinstance(node, ast.ID) and node.name == result_name
                for item in items[start + 3 :]
                for node in walk(item)
            ):
                continue
            fresh = old + "_conversion"
            names = {node.name for node in walk(function) if isinstance(node, (ast.ID, ast.Decl))}
            while fresh in names:
                fresh += "_"

            def scope(target: Any, start: int = start, fresh: str = fresh, decl: Any = decl, old: str = old) -> None:
                statements = target.block_items[start : start + 3]
                local = deepcopy(decl)
                local.name, local.init = fresh, statements[0].rvalue
                local.type.declname = fresh
                for statement in statements[1:]:
                    for node in walk(statement.rvalue):
                        if isinstance(node, ast.ID) and node.name == old:
                            node.name = fresh
                target.block_items[start : start + 3] = [ast.Compound([local, *statements[1:]])]

            yield block, scope


def propose(source: str, trial: Compared, ctx: Context) -> Iterator[Mutation]:
    from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

    cleaned = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda m: re.sub(r"[^\n]", " ", m[0]) if m[0].startswith("/") else m[0],
        source,
        flags=re.S,
    )
    try:
        tree = cdecl.parse(cleaned)
    except cdecl.ParseError:
        return  # unproved syntax stays a manual source hint
    # Edit authored syntax when available. Expanded macros are analysis inputs,
    # never a replacement source body: emitting them would discard provider
    # calls and introduce raw SDK register writes. Header typedefs seed parsing
    # without copying their declarations into the authored translation unit.
    evidence_tree = tree
    original = getattr(ctx, "source", None)
    if original is not None:
        try:
            tree = cdecl.parse(
                cdecl.declaration_source(original.read_text()),
                typedefs={node.name for node in walk(tree) if isinstance(node, c_ast.Typedef)},
            )
        except cdecl.ParseError:
            return
    functions = [node for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == trial.function]
    if len(functions) != 1:
        return
    function = functions[0]
    safety = _Safety(function, c_ast, evidence_tree)
    strategies = [(node, "conversion-scope", change) for node, change in _conversions(function, c_ast, safety)]
    strategies += [(node, "tail-duplicate", change) for node, _, change in _terminal_tails(function, c_ast, safety)]
    printer = c_generator.CGenerator()
    seen = set()
    for node, kind, change in strategies:
        if time.monotonic() >= ctx.deadline:
            return
        position = next(index for index, child in enumerate(walk(tree)) if child is node)
        made = deepcopy(tree)
        cloned = next(child for index, child in enumerate(walk(made)) if index == position)
        _replace(made, cloned, change)
        text = printer.visit(made) + "\n"
        if text not in seen:
            seen.add(text)
            yield Mutation(
                kind, "lexical conversion lifetime" if kind == "conversion-scope" else "duplicate terminal suffix", text
            )
