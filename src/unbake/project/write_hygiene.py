"""Reject direct file writes: unknown destinations may belong to a warm build."""

from __future__ import annotations

import ast
from pathlib import Path

# Lock files need stable inodes and hold no project data.
_LOCKS = {
    "cache.py": {'(path.parent / ".lock").open("a")'},
    "extract.py": {'(archive.parent / ".lock").open("a")'},
    "compilers/registry.py": {'(cache / f".{spec.id}.lock").open("a")'},
    "lock.py": {"os.open(target, os.O_RDWR | os.O_CREAT, 0o644)"},
}
# Writes that are atomic by construction: one O_APPEND line per attempt, and a tarball into the fresh
# path the cache hands its producer (the cache publishes it by rename).
_STATE: dict[str, set[str]] = {
    "work/attempts.py": {"os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)"},
    "extract.py": {'tarfile.open(destination, "w")'},
}


def violations(path: Path, content: str) -> list[str]:
    """Conservatively guard all Python destinations, including indirect paths."""
    name = path.as_posix()
    allowed = _LOCKS.get(name, set()) | _STATE.get(name, set())
    if name == "atomic.py":
        return []  # The single implementation owns fresh-file writes and copying.
    tree = ast.parse(content)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({item.asname or item.name: item.name for item in node.names})
        elif isinstance(node, ast.ImportFrom):
            aliases.update({item.asname or item.name: f"{node.module}.{item.name}" for item in node.names})
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else ""
        called = ast.unparse(func)
        first, *rest = called.split(".")
        called = ".".join([aliases.get(first, first), *rest])
        unsafe = attr in {"write_text", "write_bytes"} or called in {
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
        }
        if attr == "open" or called in {"open", "io.open", "tarfile.open"}:
            index = 1 if called in {"open", "io.open", "tarfile.open"} else 0
            modes = [node.args[index]] if len(node.args) > index else []
            modes += [item.value for item in node.keywords if item.arg == "mode"]
            for mode in modes:
                if (
                    not isinstance(mode, ast.Constant)
                    or not isinstance(mode.value, str)
                    or any(c in mode.value for c in "wa+")
                ):
                    unsafe = True
                # Exclusive creation cannot truncate an existing shared inode.
        graph_touch = attr == "touch" and name == "typemap/solver.py"
        if called in {"os.open", "os.fdopen"} or (attr == "touch" and not graph_touch):
            # One descriptor stream: storage.s mkstemp-backed JSON writer (solver.touch is a graph method).
            unsafe = not (
                name == "typemap/storage.py" and called == "os.fdopen" and ast.unparse(node.args[0]) == "descriptor"
            )
        if unsafe:
            code = ast.get_source_segment(content, node) or ""
            if code in allowed:
                ancestor: ast.AST | None = parents.get(node)
                while ancestor is not None and not isinstance(ancestor, ast.FunctionDef):
                    ancestor = parents.get(ancestor)
                if code != 'path.open("a+b")' or (isinstance(ancestor, ast.FunctionDef) and ancestor.name == "_lock"):
                    continue
            ancestor = node
            while ancestor is not None:
                if isinstance(ancestor, ast.With):
                    for item in ancestor.items:
                        context = item.context_expr
                        if (
                            isinstance(context, ast.Call)
                            and isinstance(context.func, ast.Attribute)
                            and ast.unparse(context.func) == "atomic_files.staging"
                            and isinstance(item.optional_vars, ast.Name)
                            and node.args
                            and isinstance(node.args[0], ast.Name)
                            and node.args[0].id == item.optional_vars.id
                            and called == "tarfile.open"
                        ):
                            unsafe = False
                ancestor = parents.get(ancestor)
        if unsafe:
            result.append(f"{name}:{node.lineno}: use atomic publication: {code.splitlines()[0]}")
    return result


def main() -> int:
    root = Path(__file__).parents[1]
    errors = [
        error for path in sorted(root.rglob("*.py")) for error in violations(path.relative_to(root), path.read_text())
    ]
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
