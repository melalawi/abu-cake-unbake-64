"""Step-specific code generators and selected compiler companion files.

The global compiler.sha256 manifest verifies publication. Its unrelated rows and
its own timestamp are deliberately absent from object identity.
"""

import argparse
import ast
import fcntl
import hashlib
import json
import shutil
from pathlib import Path

from unbake.project_tools.atomic import write
from unbake.project_tools.host import resolve_tool


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
    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        if node.module is not None:
            node.module = node.module.removeprefix("unbake.project_tools.")
            if node.module == "unbake.project.cache":
                node.module = "cache"
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        return None if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) else node


class _Scope(ast.NodeTransformer):
    """Project branches whose conditions are fixed by the compile kind."""

    def __init__(self, kind: str, sn64: bool):
        self.values = {"assembly": kind == "as", "sn64": sn64}
        self.unused = (
            {"compiler_for", "codegen_flags", "preprocessed_dependencies"} if kind == "as" else {"external_branches"}
        )
        if kind == "cc" and not sn64:
            self.unused.add("assembly_inputs")

    def boolean(self, node: ast.expr) -> bool | None:
        if isinstance(node, ast.Name):
            return self.values.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.boolean(node.operand)
            return None if value is None else not value
        if isinstance(node, ast.BoolOp):
            values = [self.boolean(item) for item in node.values]
            if isinstance(node.op, ast.And):
                return False if False in values else True if all(value is True for value in values) else None
            return True if True in values else False if all(value is False for value in values) else None
        return None

    def visit_If(self, node: ast.If) -> ast.AST | list[ast.AST]:
        value = self.boolean(node.test)
        if value is None:
            return self.generic_visit(node)
        result: list[ast.AST] = []
        for item in node.body if value else node.orelse:
            visited = self.visit(item)
            if isinstance(visited, list):
                result.extend(visited)
            elif visited is not None:
                result.append(visited)
        return result

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        value = self.boolean(node.test)
        return self.generic_visit(node) if value is None else self.visit(node.body if value else node.orelse)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST | None:
        return None if node.name in self.unused else self.generic_visit(node)


def driver_stamp_name(name: str, kind: str, sn64: bool) -> str:
    """Keep shared helpers shared, and project the mixed C/assembly generator."""
    return f"codegen.{kind}.{'sn64' if sn64 else 'native'}.sha256" if name == "codegen.py" else name + ".sha256"


def driver_content(path: Path, kind: str | None = None, sn64: bool = False) -> bytes:
    """Hash executable generator logic, excluding comments and linker-only ELF methods."""
    tree = ast.parse(path.read_text())
    if path.name == "elf.py":
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Object":
                node.body = [
                    item for item in node.body if not isinstance(item, ast.FunctionDef) or item.name != "relocations"
                ]
    if kind is not None and path.name == "codegen.py":
        tree = _Scope(kind, sn64).visit(tree)
    return ast.dump(_Logic().visit(tree), include_attributes=False).encode()


def binary_content(path: Path) -> str:
    """Read executable bytes, independently of timestamps or publication pins."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def publish_stamp(path: Path, content: str) -> None:
    """Replace only changed identity content, including on hardlinked copies."""
    payload = (content + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".identity.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.is_file() or path.read_bytes() != payload:
            write(path, payload)


def sync_drivers(recipe: Path) -> None:
    """Use the cache's projection for installed driver logic, ignoring comments."""
    from unbake.project.cache import key

    for kind in ("cc", "as"):
        for sn64 in (False, True):
            for name in driver_names(kind, sn64):
                path = recipe.parent / name
                if path.is_file():
                    content = key(driver_content(path, kind, sn64))
                    stamp = recipe.parent / "compile/drivers" / driver_stamp_name(name, kind, sn64)
                    publish_stamp(stamp, content)
    data = json.loads(recipe.read_text())
    if any(compiler["kind"] == "sn64" for compiler in data["compilers"].values()):
        import abumasn64

        assert abumasn64.__file__ is not None
        content = key(*(driver_content(path) for path in sorted(Path(abumasn64.__file__).parent.glob("*.py"))))
        publish_stamp(recipe.parent / "compile/drivers/abumasn64.sha256", content)


def sync_binaries(recipe: Path) -> None:
    """Observe compiler bytes before Make compares object prerequisite times."""
    data = json.loads(recipe.read_text())
    groups: dict[str, dict[str, str]] = {}
    manifest = recipe.parent / "compiler.sha256"
    if manifest.is_file():
        for row in manifest.read_text().splitlines():
            fields = row.split(maxsplit=1)
            if len(fields) == 2 and not row.startswith("#"):
                name = fields[1].lstrip("*")
                groups.setdefault(str(Path(name).parent), {})[name] = fields[0]
    for ident, compiler in data["compilers"].items():
        cc = Path(compiler["cc"])
        paths = sorted({str(cc), *selected_pins(groups, cc, recipe.parent, compiler["kind"])})
        content = json.dumps([binary_content(Path(name)) for name in paths])
        publish_stamp(recipe.parent / "compile/binaries" / (ident + ".sha256"), content)
    tools = {data["as"]} if data["as"] else set()
    tools.update(compiler["as"] for compiler in data["compilers"].values() if compiler["kind"] == "sn64")
    if data.get("cpp") and any(compiler["kind"] == "sn64" for compiler in data["compilers"].values()):
        tools.add(data["cpp"])
    for value in sorted(tools):
        executable = resolve_tool(value)
        path = Path(shutil.which(executable) or executable)
        name = hashlib.sha256(value.encode()).hexdigest()
        publish_stamp(recipe.parent / "compile/binaries" / (name + ".sha256"), binary_content(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", type=Path, required=True)
    args = parser.parse_args()
    try:
        sync_binaries(args.recipe)
        sync_drivers(args.recipe)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"HELD(identity): {error}\n")


if __name__ == "__main__":
    main()
