"""Loop form rewrites: a pointer walked beside a counter becomes an indexed access.

GCC's loop strength reduction builds a different induction variable for `p->f` with `p++`
than for `base[i].f`, which moves register choices and schedule slots; neither form can be
reached by statement reordering alone.
"""

from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any


def walk(node: Any) -> Iterator[Any]:
    """Yield a pycparser node and its descendants in preorder."""
    yield node
    for _, child in node.children():
        yield from walk(child)


def _parts(node: Any, ast: Any) -> list[Any]:
    if node is None:
        return []
    return list(node.exprs) if isinstance(node, ast.ExprList) else [node]


def _stepped(node: Any, ast: Any) -> str | None:
    """Name the variable a for-loop step advances by exactly one."""
    if isinstance(node, ast.UnaryOp) and node.op in ("p++", "++") and isinstance(node.expr, ast.ID):
        return str(node.expr.name)
    if (
        isinstance(node, ast.Assignment)
        and node.op == "+="
        and isinstance(node.lvalue, ast.ID)
        and isinstance(node.rvalue, ast.Constant)
        and node.rvalue.value == "1"
    ):
        return str(node.lvalue.name)
    return None


def _fields(pointer: str, scope: Any, ast: Any) -> list[Any]:
    """Return every `pointer->field` reference, or [] when the pointer is used any other way."""
    found = []
    for parent in walk(scope):
        for _, child in parent.children():
            if isinstance(child, ast.ID) and child.name == pointer:
                if not (isinstance(parent, ast.StructRef) and parent.name is child and parent.type == "->"):
                    return []
                found.append(parent)
    return found


def variants(function: Any, ast: Any, tree: Any) -> Iterator[tuple[Any, str, Callable[[Any], Any]]]:
    """Yield (for-loop, description, rewrite) for each pointer walked beside a zero-based counter."""
    declarations = [node for node in walk(tree) if isinstance(node, ast.Decl)]
    names = [node.name for node in walk(function) if isinstance(node, ast.Decl)]
    if len(names) != len(set(names)) or any(isinstance(node, (ast.Goto, ast.Label)) for node in walk(function)):
        return
    arrays = {node.name for node in tree.ext if isinstance(node, ast.Decl) and isinstance(node.type, ast.ArrayDecl)}
    volatile = any("volatile" in getattr(node, "quals", []) for node in walk(tree))
    if volatile:
        return
    for loop in walk(function.body):
        if not isinstance(loop, ast.For):
            continue
        starts = {
            item.lvalue.name: item.rvalue
            for item in _parts(loop.init, ast)
            if isinstance(item, ast.Assignment) and item.op == "=" and isinstance(item.lvalue, ast.ID)
        }
        stepped = [_stepped(item, ast) for item in _parts(loop.next, ast)]
        if None in stepped:
            continue
        counters = [
            name
            for name, value in starts.items()
            if name in stepped and isinstance(value, ast.Constant) and value.value == "0"
        ]
        for counter in counters:
            for pointer in (name for name in starts if name in stepped and name != counter):
                base = starts[pointer]
                if not isinstance(base, ast.ID) or base.name not in arrays:
                    continue
                pointer_decls = [node for node in declarations if node.name == pointer]
                if len(pointer_decls) != 1 or not isinstance(pointer_decls[0].type, ast.PtrDecl):
                    continue
                # A changed counter would break the relation between the index and pointer.
                if any(
                    (
                        isinstance(node, ast.Assignment)
                        and isinstance(node.lvalue, ast.ID)
                        and node.lvalue.name == counter
                    )
                    or (
                        isinstance(node, ast.UnaryOp)
                        and node.op in ("++", "p++", "--", "p--", "&")
                        and isinstance(node.expr, ast.ID)
                        and node.expr.name == counter
                    )
                    for scope in (loop.stmt, loop.cond)
                    if scope is not None
                    for node in walk(scope)
                ):
                    continue
                if any(
                    isinstance(node, ast.UnaryOp)
                    and node.op == "&"
                    and isinstance(node.expr, ast.ID)
                    and node.expr.name in (counter, pointer)
                    for node in walk(function)
                ):
                    continue
                # The rewrite drops the pointer's step, so its value must matter only as body fields.
                inside = {id(node) for node in walk(loop.stmt)}
                elsewhere = [
                    node
                    for node in walk(function.body)
                    if isinstance(node, ast.ID) and node.name == pointer and id(node) not in inside
                ]
                if len(elsewhere) != 2 or not _fields(pointer, loop.stmt, ast):
                    continue
                for form in ("address", "array", "index"):
                    yield loop, f"pointer walk to {form} form", _rewrite(ast, counter, pointer, form)


def _rewrite(ast: Any, counter: str, pointer: str, form: str) -> Callable[[Any], Any]:
    def change(loop: Any) -> Any:
        base = next(
            item.rvalue
            for item in _parts(loop.init, ast)
            if isinstance(item, ast.Assignment) and isinstance(item.lvalue, ast.ID) and item.lvalue.name == pointer
        )
        keep = form == "index"
        init = [
            item
            for item in _parts(loop.init, ast)
            if keep or not (isinstance(item, ast.Assignment) and item.lvalue.name == pointer)
        ]
        loop.init = ast.ExprList(init) if len(init) > 1 else (init[0] if init else None)
        step = [item for item in _parts(loop.next, ast) if _stepped(item, ast) != pointer]
        loop.next = ast.ExprList(step) if len(step) > 1 else (step[0] if step else None)
        holder = ast.ID(pointer) if keep else base
        for field in _fields(pointer, loop.stmt, ast):
            element = ast.ArrayRef(deepcopy(holder), ast.ID(counter))
            if form == "address":
                field.name = ast.UnaryOp("&", element)
            else:
                field.name, field.type = element, "."
        return loop

    return change
