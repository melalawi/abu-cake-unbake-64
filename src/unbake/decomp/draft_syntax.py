"""Lower MIPS address arithmetic that C does not permit on pointers."""

from typing import Any

from pycparser import c_ast, c_generator, c_parser  # type: ignore[import-untyped]

from unbake.project.config import Held


def address_arithmetic(source: str, context: str, function: str) -> str:
    # Parse once with header-backed typedefs; emit only the draft declarations.
    # pycparser does not accept comments, which carry no expression semantics.
    import re

    def clean(text: str) -> str:
        return re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: "\n" * m[0].count("\n"), text, flags=re.S)

    prefix = clean(context).rstrip() + "\n"
    try:
        tree = c_parser.CParser().parse(prefix + clean(source))
    except c_parser.ParseError as error:
        raise Held("m2c", f"{function}: unsupported C draft syntax: {error}") from error
    aliases: dict[str, Any] = {}
    tags: dict[str, Any] = {}
    values: dict[str, Any] = {}

    def resolve(node: Any) -> Any:
        seen = set()
        while isinstance(node, c_ast.TypeDecl):
            node = node.type
            if isinstance(node, c_ast.IdentifierType):
                name = " ".join(node.names)
                if name in aliases and name not in seen:
                    seen.add(name)
                    node = aliases[name]
        return node

    def pointer(node: Any) -> bool:
        return isinstance(resolve(node), (c_ast.PtrDecl, c_ast.ArrayDecl))

    def expression(node: Any) -> Any:
        if isinstance(node, c_ast.ID):
            return values.get(node.name)
        if isinstance(node, c_ast.Cast):
            return node.to_type.type
        if isinstance(node, c_ast.ArrayRef):
            base = resolve(expression(node.name))
            return base.type if pointer(base) else None
        if isinstance(node, c_ast.UnaryOp):
            base = expression(node.expr)
            if node.op == "*":
                base = resolve(base)
                return base.type if pointer(base) else None
            if node.op == "&":
                return c_ast.PtrDecl([], base)
            return base
        if isinstance(node, c_ast.StructRef):
            base = resolve(expression(node.name))
            if node.type == "->" and pointer(base):
                base = resolve(base.type)
            if isinstance(base, (c_ast.Struct, c_ast.Union)):
                base = base if base.decls else tags.get(base.name, base)
                return next((member.type for member in base.decls or [] if member.name == node.field.name), None)
        if isinstance(node, c_ast.BinaryOp) and node.op in ("+", "-"):
            left, right = expression(node.left), expression(node.right)
            if pointer(left) and not pointer(right):
                return left
            if node.op == "+" and pointer(right) and not pointer(left):
                return right
        return None

    def cast(node: Any, spelling: str) -> Any:
        return c_ast.Cast(
            c_ast.Typename(None, [], None, c_ast.TypeDecl(None, [], None, c_ast.IdentifierType([spelling]))), node
        )

    class Lower(c_ast.NodeVisitor):  # type: ignore[misc]
        changed = False

        def visit_Typedef(self, node: Any) -> None:
            aliases[node.name] = node.type
            self.generic_visit(node)

        def visit_Struct(self, node: Any) -> None:
            if node.name and node.decls:
                tags[node.name] = node
            self.generic_visit(node)

        visit_Union = visit_Struct

        def visit_Decl(self, node: Any) -> None:
            values[node.name] = node.type
            self.generic_visit(node)

        def visit_BinaryOp(self, node: Any) -> None:
            self.generic_visit(node)
            left, right = pointer(expression(node.left)), pointer(expression(node.right))
            if node.op in ("&", "|", "^", "<<", ">>"):
                if left:
                    node.left = cast(node.left, "u32")
                if right:
                    node.right = cast(node.right, "u32")
                self.changed |= left or right
            elif node.op == "-" and right and not left:
                node.right = cast(node.right, "s32")
                self.changed = True

    lower = Lower()
    lower.visit(tree)
    if not lower.changed:
        return source
    first_line = prefix.count("\n") + 1
    generator = c_generator.CGenerator()
    return (
        "\n".join(
            generator.visit(node) + ("" if isinstance(node, c_ast.FuncDef) else ";")
            for node in tree.ext
            if node.coord and node.coord.line >= first_line
        )
        + "\n"
    )
