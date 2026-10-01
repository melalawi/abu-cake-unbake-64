"""Conservative C structure mutations; compilation and scoring belong to core."""

import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from copy import deepcopy
from itertools import pairwise
from typing import Any, cast

from unbake.decomp.trial import Trial
from unbake.project.config import Held
from unbake.search.core import Context, Mutation
from unbake.search.loops import variants as loop_variants
from unbake.search.loops import walk


def _items(node: Any, ast: Any) -> list[Any]:
    return cast(list[Any], node.block_items or []) if isinstance(node, ast.Compound) else [node]


def _identifier(node: Any, ast: Any) -> str | None:
    return cast(str, node.name) if isinstance(node, ast.ID) else None


class _Safety:
    def __init__(self, function: Any, ast: Any, tree: Any) -> None:
        self.ast = ast
        declarations = [node for node in walk(function.body) if isinstance(node, ast.Decl)]
        params = function.decl.type.args
        if params:
            declarations += [node for node in params.params if isinstance(node, ast.Decl)]
        counts = Counter(node.name for node in declarations)
        self.shadowed = any(count > 1 for count in counts.values())
        self.types = {node.name: node for node in declarations}
        self.escaped = {
            node.expr.name
            for node in walk(function.body)
            if isinstance(node, ast.UnaryOp) and node.op == "&" and isinstance(node.expr, ast.ID)
        }
        volatile_types = {
            node.name
            for node in walk(tree)
            if isinstance(node, ast.Typedef) and any("volatile" in getattr(child, "quals", []) for child in walk(node))
        }
        while True:
            inherited = {
                node.name
                for node in walk(tree)
                if isinstance(node, ast.Typedef)
                and any(
                    isinstance(child, ast.IdentifierType) and set(child.names) & volatile_types for child in walk(node)
                )
            }
            if inherited <= volatile_types:
                break
            volatile_types |= inherited
        self.volatile_members = any(
            isinstance(node, ast.Struct) and any("volatile" in getattr(child, "quals", []) for child in walk(node))
            for node in walk(tree)
        )
        self.volatile = {
            decl.name
            for decl in declarations
            if any(
                "volatile" in getattr(node, "quals", [])
                or (isinstance(node, ast.IdentifierType) and set(node.names) & volatile_types)
                for node in walk(decl)
            )
        }

    def effects(self, node: Any) -> tuple[set[str | None], set[str | None], bool]:
        ast = self.ast
        reads: set[str | None] = set()
        writes: set[str | None] = set()
        barrier = self.shadowed

        def location(expr: Any) -> str | None:
            nonlocal barrier
            if isinstance(expr, ast.ID) and expr.name in self.types:
                if expr.name in self.escaped or expr.name in self.volatile:
                    barrier = True
                return cast(str, expr.name)
            if isinstance(expr, ast.StructRef) and expr.type == ".":
                if self.volatile_members:
                    barrier = True
                return location(expr.name)
            barrier = True
            return None

        def visit(expr: Any) -> None:
            nonlocal barrier
            if expr is None:
                return
            if isinstance(expr, ast.Assignment):
                dest = location(expr.lvalue)
                writes.add(dest)
                if expr.op != "=":
                    reads.add(dest)
                visit(expr.rvalue)
            elif isinstance(expr, (ast.ID, ast.StructRef)):
                reads.add(location(expr))
            elif isinstance(expr, ast.Constant):
                pass
            elif isinstance(expr, (ast.BinaryOp, ast.Cast, ast.TernaryOp)):
                for _, child in expr.children():
                    if not isinstance(child, ast.Typename):
                        visit(child)
            elif isinstance(expr, ast.UnaryOp) and expr.op in ("+", "-", "!", "~"):
                visit(expr.expr)
            else:
                barrier = True

        visit(node)
        return reads, writes, barrier

    def value_type(self, node: Any) -> str | None:
        ast = self.ast
        if isinstance(node, ast.Constant):
            return cast(str, node.type)
        if isinstance(node, ast.ID) and node.name in self.types:
            decl = self.types[node.name]
            if isinstance(decl.type, ast.TypeDecl) and isinstance(decl.type.type, ast.IdentifierType):
                return " ".join(decl.type.type.names)
        if isinstance(node, ast.UnaryOp) and node.op in ("+", "-", "!", "~"):
            return "int" if node.op == "!" else self.value_type(node.expr)
        if isinstance(node, ast.BinaryOp):
            if node.op in ("<", ">", "<=", ">=", "==", "!=", "&&", "||"):
                return "int"
            left, right = self.value_type(node.left), self.value_type(node.right)
            if left == right:
                return left
        return None

    def same_value_type(self, left: Any, right: Any) -> bool:
        return self.value_type(left) is not None and self.value_type(left) == self.value_type(right)

    def pure(self, node: Any) -> bool:
        _, writes, barrier = self.effects(node)
        return not writes and not barrier

    def independent(self, left: Any, right: Any) -> bool:
        lr, lw, lb = self.effects(left)
        rr, rw, rb = self.effects(right)
        return not (lb or rb or lw & (rr | rw) or rw & lr)


def _variants(function: Any, ast: Any, printer: Any, tree: Any) -> Iterator[tuple[Any, str, Callable[[Any], Any]]]:
    safety = _Safety(function, ast, tree)
    yield from loop_variants(function, ast, tree)
    for node in walk(function.body):
        if (
            isinstance(node, ast.BinaryOp)
            and node.op == "+"
            and safety.pure(node.left)
            and safety.pure(node.right)
            and any(
                isinstance(operand, ast.ID)
                and operand.name in safety.types
                and isinstance(safety.types[operand.name].type, ast.PtrDecl)
                for operand in (node.left, node.right)
            )
        ):

            def operands(target: Any) -> None:
                target.left, target.right = target.right, target.left

            # In valid C, pointer addition's other operand is an integer.
            yield node, "pointer addition operand order", operands
        if isinstance(node, ast.Compound):
            items = node.block_items or []
            for index, (left, right) in enumerate(pairwise(items)):
                if safety.independent(left, right):

                    def swap(target: Any, index: int = index) -> None:
                        target.block_items[index : index + 2] = reversed(target.block_items[index : index + 2])

                    yield node, "statement order", swap
                if isinstance(left, ast.If) and left.iftrue and left.iffalse:
                    # Copy a suffix into both branches only if evaluating the condition is pure.
                    branches = _items(left.iftrue, ast) + _items(left.iffalse, ast)
                    if (
                        safety.pure(left.cond)
                        and isinstance(right, ast.Assignment)
                        and not any(
                            isinstance(child, (ast.Goto, ast.Label, ast.Return, ast.Break, ast.Continue, ast.Decl))
                            for branch in branches
                            for child in walk(branch)
                        )
                    ):

                        def expand(target: Any, index: int = index) -> None:
                            branch, suffix = target.block_items[index : index + 2]
                            branch.iftrue = ast.Compound([*_items(branch.iftrue, ast), deepcopy(suffix)])
                            branch.iffalse = ast.Compound([*_items(branch.iffalse, ast), deepcopy(suffix)])
                            del target.block_items[index + 1]

                        yield node, "expand shared tail", expand
            # Expand a terminating, same-scope labelled tail at each incoming jump.
            for label_index, label in enumerate(items):
                if not isinstance(label, ast.Label):
                    continue
                tail = [label.stmt, *items[label_index + 1 :]]
                if not tail or not isinstance(tail[-1], (ast.Return, ast.Goto)):
                    continue
                if any(isinstance(child, (ast.Decl, ast.Label)) for statement in tail for child in walk(statement)):
                    continue
                jumps = [
                    child
                    for statement in items[:label_index]
                    for child in walk(statement)
                    if isinstance(child, ast.Goto) and child.name == label.name
                ]
                all_jumps = [
                    child for child in walk(function.body) if isinstance(child, ast.Goto) and child.name == label.name
                ]
                if not jumps or len(jumps) != len(all_jumps):
                    continue
                if any(
                    isinstance(child, ast.Decl)
                    for statement in items[:label_index]
                    for child in walk(statement)
                    if child not in items
                ):
                    continue
                for jump in jumps:

                    def duplicate(target: Any, tail: list[Any] = tail) -> Any:
                        return ast.Compound(deepcopy(tail))

                    yield jump, "expand labelled tail", duplicate
            # Shorten an uninitialised scalar's scope to its first assignment and remaining uses.
            for index, decl in enumerate(items):
                if (
                    not isinstance(decl, ast.Decl)
                    or decl.init is not None
                    or decl.storage
                    or not isinstance(decl.type, ast.TypeDecl)
                    or decl.name in safety.escaped
                    or decl.name in safety.volatile
                    or safety.shadowed
                ):
                    continue
                uses = [
                    pos
                    for pos in range(index + 1, len(items))
                    if any(isinstance(child, ast.ID) and child.name == decl.name for child in walk(items[pos]))
                ]
                if not uses:
                    continue
                first = uses[0]
                assign = items[first]
                if (
                    not isinstance(assign, ast.Assignment)
                    or assign.op != "="
                    or _identifier(assign.lvalue, ast) != decl.name
                    or any(isinstance(child, ast.ID) and child.name == decl.name for child in walk(assign.rvalue))
                    or any(isinstance(child, (ast.Goto, ast.Label)) for child in walk(node))
                ):
                    continue

                def scope(target: Any, index: int = index, first: int = first) -> None:
                    local = target.block_items[index]
                    local.init = target.block_items[first].rvalue
                    tail = target.block_items[first + 1 :]
                    target.block_items[first:] = [ast.Compound([local, *tail])]
                    del target.block_items[index]

                yield node, "shorten local lifetime", scope
        elif isinstance(node, ast.If) and node.iftrue and node.iffalse:

            def invert(target: Any) -> None:
                target.cond = ast.UnaryOp("!", target.cond)
                target.iftrue, target.iffalse = target.iffalse, target.iftrue

            yield node, "if/else layout", invert
            left, right = _items(node.iftrue, ast), _items(node.iffalse, ast)
            if (
                len(left) == len(right) == 1
                and isinstance(left[0], ast.Assignment)
                and isinstance(right[0], ast.Assignment)
                and left[0].op == right[0].op == "="
                and isinstance(left[0].lvalue, ast.ID)
                and printer.visit(left[0].lvalue) == printer.visit(right[0].lvalue)
                and safety.pure(node.cond)
                and safety.pure(left[0].rvalue)
                and safety.pure(right[0].rvalue)
                and safety.same_value_type(left[0].rvalue, right[0].rvalue)
            ):

                def conditional(target: Any) -> Any:
                    yes, no = _items(target.iftrue, ast)[0], _items(target.iffalse, ast)[0]
                    return ast.Assignment("=", yes.lvalue, ast.TernaryOp(target.cond, yes.rvalue, no.rvalue))

                yield node, "if/else to ternary", conditional
        elif isinstance(node, ast.TernaryOp):

            def arms(target: Any) -> None:
                target.cond = ast.UnaryOp("!", target.cond)
                target.iftrue, target.iffalse = target.iffalse, target.iftrue

            # Swapping arms keeps the conditional's type; arm order decides what GCC evaluates
            # into an outgoing argument slot first.
            yield node, "conditional arm order", arms
        if (
            isinstance(node, ast.Assignment)
            and node.op == "="
            and isinstance(node.rvalue, ast.TernaryOp)
            and isinstance(node.lvalue, ast.ID)
            and safety.same_value_type(node.rvalue.iftrue, node.rvalue.iffalse)
        ):

            def branch(target: Any) -> Any:
                choice = target.rvalue
                return ast.If(
                    choice.cond,
                    ast.Compound([ast.Assignment("=", deepcopy(target.lvalue), choice.iftrue)]),
                    ast.Compound([ast.Assignment("=", target.lvalue, choice.iffalse)]),
                )

            yield node, "ternary to if/else", branch


def _replace(root: Any, wanted: Any, change: Callable[[Any], Any]) -> None:
    """Apply one edit to the corresponding node in a cloned tree."""
    for parent in walk(root):
        for name, child in parent.children():
            if child is wanted:
                replacement = change(child)
                if replacement is not None:
                    match = re.fullmatch(r"(\w+)\[(\d+)\]", name)
                    if match:
                        getattr(parent, match[1])[int(match[2])] = replacement
                    else:
                        setattr(parent, name, replacement)
                return


def propose(source: str, trial: Trial, ctx: Context) -> Iterator[Mutation]:
    """Yield unique structural alternatives; optional context.focus_lines ranks edits."""
    if not isinstance(source, str) or not source.strip():
        raise Held("order", "source is required as C text")
    function_name = getattr(trial, "function", None)
    if not function_name:
        raise Held("order", "trial.function is required")
    if ctx is None:
        raise Held("order", "context is required")
    deadline = getattr(ctx, "deadline", None)
    if type(deadline) not in (int, float) or not math.isfinite(cast(float, deadline)):
        raise Held("order", "context.deadline: finite monotonic time required")
    deadline = cast(float, deadline)
    if time.monotonic() >= deadline:
        return
    try:
        from pycparser import c_ast, c_generator, c_parser  # type: ignore[import-untyped]
    except ImportError as error:
        raise Held("order", "pycparser is required") from error
    # Comments are whitespace; keep string literals byte-for-byte.
    cleaned = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: match[0] if match[0][0] in ('"', "'") else re.sub(r"[^\n]", " ", match[0]),
        source,
        flags=re.S,
    )
    if re.search(r"^\s*#", cleaned, re.M):
        raise Held("order", "source.preprocessed is required (directives remain)")
    try:
        tree = c_parser.CParser().parse(cleaned)
    except (c_parser.ParseError, AssertionError) as error:
        raise Held("order", f"source.syntax: {error}") from error
    functions = [node for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == function_name]
    if len(functions) != 1:
        raise Held("order", f"trial.function {function_name}: exactly one definition required")
    printer = c_generator.CGenerator()
    seen: set[str] = set()
    comments = re.findall(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', source, re.S)
    comments = [comment for comment in comments if comment.startswith(("/",))]
    variants = list(_variants(functions[0], c_ast, printer, tree))
    focus = getattr(ctx, "focus_lines", None)
    if focus is not None:
        if not isinstance(focus, (tuple, list)) or any(type(line) is not int or line < 1 for line in focus):
            raise Held("order", "context.focus_lines: positive source line numbers required")
        variants.sort(key=lambda row: min((abs(row[0].coord.line - line) for line in focus), default=0))
    for target, description, change in variants:
        if time.monotonic() >= deadline:
            return
        made = deepcopy(tree)
        # Preorder traversal is stable across a deepcopy.
        position = next(index for index, node in enumerate(walk(tree)) if node is target)
        cloned = next(node for index, node in enumerate(walk(made)) if index == position)
        _replace(made, cloned, change)
        text = "".join(comment + "\n" for comment in comments) + printer.visit(made) + "\n"
        if text not in seen:
            seen.add(text)
            yield Mutation("order", description, text)
