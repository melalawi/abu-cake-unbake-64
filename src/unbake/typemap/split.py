"""Guarded shared declarations with explicit, transitive type prerequisites."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

from unbake.decomp.draft_context import ordered_headers
from unbake.decomp.header_declarations import declaration_source, declarations
from unbake.project.config import Held
from unbake.typemap.header_names import alias_types

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)


class Layout:
    """One component per declaration home; pointer cycles share a guarded cluster."""

    def __init__(
        self,
        contents: dict[Path, str],
        rendered: dict[Path, str],
        root: Path,
        *,
        aliases: dict[str, str] | None = None,
        render: Callable[[Path, str], bytes] | None = None,
    ):
        self.aliases = {name: target for text in contents.values() for name, target in alias_types(text).items()}
        self.aliases.update(aliases or {})
        self.root = root
        self.render = render or guarded
        self.contents = contents
        self.parsed = {path: declarations(text) for path, text in contents.items()}
        self.providers: dict[str, set[Path]] = {}
        self.tags: dict[str, set[Path]] = {}
        for path, row in self.parsed.items():
            for name in row.typedefs | row.exports:
                self.providers.setdefault(name, set()).add(path)
            for name in row.tags:
                self.tags.setdefault(name, set()).add(path)
        self.dependencies = {path: set[Path]() for path in contents}
        resolved = {path.resolve(): path for path in contents}
        for path, row in self.parsed.items():
            deps = self.dependencies[path]
            expression_types = set(re.findall(r"\bsizeof\s*\(\s*([A-Za-z_]\w*)", declaration_source(contents[path])))
            for name in (row.uses | expression_types) - row.typedefs:
                deps.update(self.providers.get(name, set()))
            # Full definitions are useful to bodies that dereference signature
            # pointers. A bare forward/alias needs only a file-scope tag.
            text = declaration_source(contents[path])
            if "{" in text or row.exports - row.tags or "(" in text:
                for name in re.findall(r"\b(?:struct|union|enum)\s+(\w+)", text):
                    if name not in row.tags:
                        deps.update(self.tags.get(name, set()))
            for name in _INCLUDE.findall(contents[path]):
                if name in ("typemap.h", "shared/typemap.h", "prototypes.h", "shared/prototypes.h"):
                    continue
                target = next(
                    (resolved[p.resolve()] for p in (path.parent / name, root / name) if p.resolve() in resolved),
                    None,
                )
                if target is not None:
                    deps.add(target)
            for alias in row.uses:
                alias_target = self.aliases.get(alias, "")
                if re.fullmatch(r"(?:struct|union) \w+", alias_target):
                    deps.update(self.tags.get(alias_target.split()[1], set()))
            deps.discard(path)
        self.headers: dict[Path, bytes] = {}
        self.homes: dict[Path, Path] = {}
        self.groups = self._clusters()
        for group in self.groups:
            key = "\n".join(str(path.relative_to(root)) for path in sorted(group))
            label = re.sub(r"[^A-Za-z0-9_]", "_", sorted(group)[0].stem).strip("_")[:60] or "context"
            name = label + "_" + hashlib.sha256(key.encode()).hexdigest()[:12]
            destination = root / "shared/types" / (name + ".h")
            for path in group:
                self.homes[path] = destination
        for group in self.groups:
            destination = self.homes[next(iter(group))]
            dependencies = {self.homes[dep] for path in group for dep in self.dependencies[path] if dep not in group}
            lines = [self.include(path) for path in sorted(dependencies)]
            tags = {
                match[0]
                for path in group
                for match in re.finditer(r"\b(?:struct|union)\s+[A-Za-z_]\w*", declaration_source(contents[path]))
            }
            lines.extend(tag + ";" for tag in sorted(tags))
            for path in ordered_headers({path: contents[path] for path in sorted(group)}, aliases=self.aliases):
                # An authored consumer can import its own former umbrella. Its
                # prerequisites are now explicit, so that edge must disappear.
                lines.append(
                    _INCLUDE.sub(lambda m: "" if m[1] in ("shared/typemap.h", "typemap.h") else m[0], rendered[path])
                )
            self.headers[destination] = self.render(destination, "\n".join(lines))

    def _clusters(self) -> list[set[Path]]:
        index: dict[Path, int] = {}
        low: dict[Path, int] = {}
        stack: list[Path] = []
        active: set[Path] = set()
        groups: list[set[Path]] = []

        def visit(path: Path) -> None:
            index[path] = low[path] = len(index)
            stack.append(path)
            active.add(path)
            for dep in sorted(self.dependencies[path]):
                if dep not in index:
                    visit(dep)
                    low[path] = min(low[path], low[dep])
                elif dep in active:
                    low[path] = min(low[path], index[dep])
            if low[path] == index[path]:
                group = set()
                while True:
                    member = stack.pop()
                    active.remove(member)
                    group.add(member)
                    if member == path:
                        break
                groups.append(group)

        for path in self.contents:
            if path not in index:
                visit(path)
        return groups

    def include(self, path: Path) -> str:
        from unbake.typemap.storage import relative_root

        return f'#include "{relative_root(self.root, path)}"'

    def required(
        self, text: str, *, blocked: set[str] | None = None, blocked_tags: set[str] | None = None
    ) -> set[Path]:
        """Select body/signature type names, then let header includes close them."""
        selected = required_providers(text, self.providers, self.tags, self.aliases, blocked, blocked_tags)
        return {self.homes[path] for path in selected}

    def consumer(self, path: Path, text: str) -> bytes:
        includes = "\n".join(self.include(home) for home in sorted(self.required(text)))
        return self.render(path, includes + "\n" + text)

    def umbrella(self, path: Path, *, excluded: set[Path] | None = None) -> bytes:
        return self.render(
            path, "\n".join(self.include(home) for home in sorted(set(self.headers) - (excluded or set())))
        )


def required_providers(
    text: str,
    providers: dict[str, set[Path]],
    tags: dict[str, set[Path]],
    aliases: dict[str, str],
    blocked: set[str] | None = None,
    blocked_tags: set[str] | None = None,
) -> set[Path]:
    """Resolve one consumer's names; both generation and imported C use this closure."""
    selected: set[Path] = set()
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", declaration_source(text))
    pending = re.findall(r"\b[A-Za-z_]\w*\b", code)
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in (blocked or set()):
            selected.update(providers.get(name, set()))
        if name not in (blocked_tags or set()):
            selected.update(tags.get(name, set()))
        pending.extend(re.findall(r"\b[A-Za-z_]\w*\b", aliases.get(name, "")))
    return selected


def guarded(path: Path, text: str) -> bytes:
    guard = "UNBAKE_" + re.sub(r"[^A-Za-z0-9]", "_", path.name).upper()
    return f"#ifndef {guard}\n#define {guard}\n{text}\n#endif\n".encode()


def statements(text: str) -> list[str]:
    """Separate legacy declarations, retaining complete conditional blocks.

    This accepts a header body, without its outer include guard. Directives at
    file scope remain individual units; conditional branches remain together.
    """
    cleaned = declaration_source(text)
    tokens = list(re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{};]|\S', cleaned))
    directives = list(re.finditer(r"^[ \t]*#(?:\\\n|[^\n])*", text, re.M))
    events = sorted([(m.start(), "token", m) for m in tokens] + [(m.start(), "directive", m) for m in directives])
    depth = conditional = 0
    start = None
    result = []
    for offset, kind, match in events:
        if start is None:
            start = offset
        if kind == "directive":
            directive = re.match(r"#\s*(\w+)", match[0].lstrip())
            assert directive is not None
            if directive[1] in ("if", "ifdef", "ifndef"):
                conditional += 1
            elif directive[1] == "endif":
                conditional -= 1
                if conditional < 0:
                    raise Held("solve", "types.split: unmatched endif")
            if not conditional and not depth:
                result.append(text[start : match.end()])
                start = None
        else:
            depth += (match[0] == "{") - (match[0] == "}")
            if match[0] == ";" and not depth and not conditional:
                result.append(text[start : match.end()])
                start = None
    if depth or conditional or start is not None:
        raise Held("solve", "types.split: incomplete declaration or conditional")
    return result


def narrow(text: str, includes: str) -> str:
    """Replace legacy or previous split imports once, retaining local macros."""
    text = re.sub(
        r"^\s*#if\s+defined\(UNBAKE_CONSUMER_[A-F0-9]+\).*?^\s*#endif[^\n]*(?:\n|$)", "", text, flags=re.M | re.S
    )
    pattern = re.compile(
        r'^\s*#\s*include\s*"(?:(?:shared/)?typemap.h|shared/(?:types|consumers)/[^"\n]+)"[^\n]*', re.M
    )
    first = True

    def replace(match: re.Match[str]) -> str:
        nonlocal first
        if first:
            first = False
            return includes
        return ""

    return pattern.sub(replace, text)


def consumer_macro(stem: str) -> str:
    return "UNBAKE_CONSUMER_" + hashlib.sha256(stem.encode()).hexdigest()[:16].upper()
