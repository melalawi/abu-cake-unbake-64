"""Step-specific code generators and selected compiler companion files.

The global compiler.sha256 manifest verifies publication. Its unrelated rows and
its own timestamp are deliberately absent from object identity.
"""

import ast
from pathlib import Path


def driver_names(kind: str, sn64: bool) -> tuple[str, ...]:
    names: tuple[str, ...] = ("codegen.py",)
    if kind == "cc":
        names += ("elf.py",)
    if sn64:
        names += ("sn64_cc.py",)
        if kind == "as":
            names += ("resolve_external_branches.py",)
    return names


def selected_pins(groups: dict[str, dict[str, str]], cc: Path, tools: Path, kind: str | None = None) -> dict[str, str]:
    if cc.parent.absolute() == tools.absolute():
        # A compiler directly in tools must not absorb generated helper pins.
        return {str(cc): groups.get(str(cc.parent), {})[str(cc)]} if str(cc) in groups.get(str(cc.parent), {}) else {}
    # IDO's C pipeline does not invoke Pascal/C++ frontends, diagnostic catalogs,
    # target runtime libraries, or the bundled executable linker/report tools.
    c_pipeline = {"acpp", "as0", "as1", "cc", "cfe", "copt", "ugen", "ujoin", "uld", "umerge", "uopt", "usplit"}
    return {
        name: digest
        for parent, entries in groups.items()
        if Path(parent).is_relative_to(cc.parent)
        for name, digest in entries.items()
        if kind != "ido" or Path(name).name in c_pipeline or Path(name) == cc
    }


class _Logic(ast.NodeTransformer):
    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        return None if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) else node


def driver_content(path: Path) -> bytes:
    """Hash executable generator logic, excluding comments and linker-only ELF methods."""
    tree = ast.parse(path.read_text())
    if path.name == "elf.py":
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Object":
                node.body = [
                    item for item in node.body if not isinstance(item, ast.FunctionDef) or item.name != "relocations"
                ]
    return ast.dump(_Logic().visit(tree), include_attributes=False).encode()
