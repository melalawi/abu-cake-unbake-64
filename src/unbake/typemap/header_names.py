"""Keep source-owned names and draft placeholders out of shared declarations."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from unbake.decomp.header_declarations import Parser, attribute_source, declaration_source
from unbake.project.config import Held, Policy, Project

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\.\.\.|\S')
_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)
_PLACEHOLDER = re.compile(r"M2C_UNK\d*\Z")


def placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(name))


class _Declarations(Parser):
    def __init__(self, source: str, replacements: dict[str, str] | None = None, blocked: set[str] | None = None):
        super().__init__(source)
        self.matches = list(_TOKEN.finditer(attribute_source(declaration_source(source))))
        self.tokens = [match[0] for match in self.matches]
        self.source = source
        self.replacements = replacements or {}
        self.blocked = blocked or set()
        self.edits: list[tuple[int, int, str]] = []
        self.depth = 0
        self.names: set[str] = set()
        self.alias_types: dict[str, str] = {}
        self.external_names: set[str] = set()
        self.declarator_depth = 0

    def declarator(self, *, abstract: bool = False) -> tuple[str, bool]:
        self.declarator_depth += 1
        try:
            name, pointer = super().declarator(abstract=abstract)
            if self.depth == 1 and self.declarator_depth == 1 and name:
                self.external_names.add(name)
            return name, pointer
        finally:
            self.declarator_depth -= 1

    def specifiers(self) -> tuple[str, str]:
        start = self.index
        while self.tokens[start] in ("const", "volatile", "restrict", "__restrict", "__restrict__"):
            start += 1
        if (
            self.depth >= 1
            and self.tokens[start] in ("struct", "union", "enum")
            and start + 2 < len(self.tokens)
            and re.fullmatch(r"[A-Za-z_]\w*", self.tokens[start + 1])
            and self.tokens[start + 2] in ("{", ";")
        ):
            self.names.add(self.tokens[start + 1])
        tag, alias = super().specifiers()
        if placeholder(alias) and alias not in self.replacements and self.replacements:
            raise Held("solve", f"types.header_parse: {alias}: missing concrete placeholder type")
        if alias in self.replacements:
            match = self.matches[start]
            self.edits.append((match.start(), match.end(), self.replacements[alias]))
        return tag, alias

    def skip(self, stops: set[str]) -> None:
        begin = self.index
        super().skip(stops)
        # Extents and initializers may use a typedef in sizeof or a cast. Value
        # identifiers (including fields with an alias's name) stay untouched.
        for index in range(begin, self.index):
            name = self.tokens[index]
            if name not in self.replacements:
                continue
            prefix = self.tokens[begin:index]
            if not prefix or prefix[-1] not in ("(", "sizeof", "const", "volatile"):
                continue
            match = self.matches[index]
            edit = (match.start(), match.end(), self.replacements[name])
            if edit not in self.edits:
                self.edits.append(edit)

    def section(self, start: int, end: int) -> str:
        begin, finish = self.matches[start].start(), self.matches[end - 1].end()
        text = self.source[begin:finish]
        for left, right, value in sorted(self.edits, reverse=True):
            if begin <= left < right <= finish:
                text = text[: left - begin] + value + text[right - begin :]
        return text

    def declaration(self, *, external: bool = False) -> None:
        self.depth += 1
        try:
            if not external or self.peek() != "typedef":
                super().declaration(external=external)
                return
            begin = self.index
            self.take("typedef")
            spec = self.index
            self.specifiers()
            spec_end = self.index
            members = []
            removed = False
            removed_names = []
            while True:
                start = self.index
                name, _ = self.declarator()
                self.result.typedefs.add(name)
                self.names.add(name)
                spelling = self.section(spec, spec_end)
                if "{" in self.tokens[spec:spec_end]:
                    spelling = spelling.split("{", 1)[0].strip()
                    if spelling in ("struct", "union", "enum"):
                        spelling += " " + name
                abstract = re.sub(rf"\b{re.escape(name)}\b", "", self.section(start, self.index), count=1)
                self.alias_types[name] = (spelling + " " + abstract).strip()
                if name in self.blocked or placeholder(name):
                    removed = True
                    removed_names.append(name)
                else:
                    members.append(self.section(start, self.index))
                if self.peek() != ",":
                    break
                self.take(",")
            self.take(";")
            if removed:
                specifier = self.section(spec, spec_end)
                if re.search(r"\b(?:struct|union|enum)\s*\{", specifier):
                    target = self.replacements.get(removed_names[0], self.alias_types[removed_names[0]])
                    tag = re.search(r"\b(?:struct|union|enum)\s+\w+", target)
                    if tag is not None:
                        specifier = re.sub(r"\b(?:struct|union|enum)(?=\s*\{)", tag[0], specifier, count=1)
                replacement = "typedef " + specifier + " " + ", ".join(members) + ";" if members else ""
                # Retain a named aggregate definition even when its aliases are source-owned.
                if not members and "{" in self.tokens[spec:spec_end]:
                    replacement = specifier + ";"
                left, right = self.matches[begin].start(), self.matches[self.index - 1].end()
                self.edits = [edit for edit in self.edits if not left <= edit[0] < right]
                self.edits.append((left, right, replacement))
        finally:
            self.depth -= 1


def alias_types(source: str) -> dict[str, str]:
    parser = _Declarations(source)
    try:
        parser.parse()
    except Held as error:
        raise Held("solve", f"types.header_parse: {error.reason}") from error
    return parser.alias_types


def type_identity(type_: str, aliases: dict[str, str]) -> object:
    """Compare declarator structure, not parameter names or typedef spelling."""
    from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

    from unbake.typemap.declarations import _SeededParser, canonical, declarator

    def parse(spelling: str) -> Any:
        parser = _SeededParser(dict.fromkeys(aliases, True))
        return parser.parse("typedef " + declarator(spelling, "__type_identity") + ";").ext[0].type

    def shape(node: Any, active: tuple[str, ...] = ()) -> object:
        if isinstance(node, c_ast.TypeDecl):
            target = shape(node.type, active)
            if not node.quals:
                return target
            if isinstance(target, tuple) and target[0] in ("qualified", "pointer"):
                return (target[0], tuple(sorted(set(node.quals) | set(target[1]))), target[2])
            return ("qualified", tuple(sorted(node.quals)), target)
        if isinstance(node, c_ast.IdentifierType):
            name = " ".join(node.names)
            if name in aliases and name not in active:
                return shape(parse(aliases[name]), (*active, name))
            name = " ".join(node.names)
            words = node.names
            if set(words) <= {"signed", "unsigned", "short", "long", "int"}:
                width = "short" if "short" in words else "long " * words.count("long")
                name = ("unsigned " if "unsigned" in words else "") + (width.strip() or "int")
            return ("scalar", canonical(name, {}))
        if isinstance(node, (c_ast.Struct, c_ast.Union, c_ast.Enum)):
            return (type(node).__name__, node.name)
        if isinstance(node, c_ast.PtrDecl):
            return ("pointer", tuple(sorted(node.quals)), shape(node.type, active))
        if isinstance(node, c_ast.ArrayDecl):
            return ("array", c_generator.CGenerator().visit(node.dim) if node.dim else "", shape(node.type, active))
        if isinstance(node, c_ast.FuncDecl):
            params = None if node.args is None else tuple(parameter(param, active) for param in node.args.params)
            return ("function", shape(node.type, active), params)
        if isinstance(node, c_ast.EllipsisParam):
            return ("variadic",)
        raise Held("solve", "types.header_parse: unsupported compatible declarator")

    def parameter(node: Any, active: tuple[str, ...]) -> object:
        if isinstance(node, c_ast.EllipsisParam):
            return shape(node, active)
        value = shape(node.type, active)
        # C adjusts parameter arrays/functions to pointers and drops top-level qualifiers.
        if isinstance(value, tuple):
            if value[0] == "array":
                return ("pointer", (), value[2])
            if value[0] == "function":
                return ("pointer", (), value)
            if value[0] == "qualified":
                return value[2]
            if value[0] == "pointer":
                return ("pointer", (), value[2])
        return value

    try:
        return shape(parse(type_))
    except Exception as error:
        raise Held("solve", f"types.header_parse: compatible declarator: {error}") from error


def resolve(type_: str, replacements: dict[str, str]) -> str:
    """Expand type aliases without treating tag names as typedef uses."""

    def replace(match: re.Match[str]) -> str:
        if re.search(r"\b(?:struct|union|enum)\s+$", type_[: match.start()]):
            return match[0]
        return replacements.get(match[0], match[0])

    return re.sub(r"\b[A-Za-z_]\w*\b", replace, type_)


def rewrite(source: str, replacements: dict[str, str], blocked: set[str]) -> str:
    parser = _Declarations(source, replacements, blocked)
    try:
        parser.parse()
    except Held as error:
        raise Held("solve", f"types.header_parse: {error.reason}") from error
    for start, end, value in sorted(parser.edits, reverse=True):
        source = source[:start] + value + source[end:]
    return source


class IncludeClosure:
    """Resolve project-local includes once, sharing the effective input texts."""

    def __init__(self, project: Project, texts: dict[Path, str] | None = None):
        self.canonical: dict[str, str] = {}
        self.texts = {self.resolved(str(path)): text for path, text in (texts or {}).items()}
        self.supplied = set(self.texts)
        self.edges: dict[str, set[str]] = {}
        self.frontiers: dict[str, set[str]] = {}
        self.roots = tuple(map(str, project.include))
        shared = self.resolved(str(project.include[0] / "shared"))
        self.special = {
            os.path.join(shared, name) for name in ("typemap.h", "prototypes.h", "types", "decls", "consumers")
        }
        self.prefixes = tuple(os.path.join(shared, name) + os.sep for name in ("types", "decls", "consumers"))

    def resolved(self, path: str) -> str:
        if path not in self.canonical:
            self.canonical[path] = os.path.realpath(path)
        return self.canonical[path]

    def generated(self, path: str) -> bool:
        return path in self.special or path.startswith(self.prefixes)

    def imports(self, path: str) -> set[str]:
        path = self.resolved(path)
        if path not in self.edges:
            if path not in self.texts:
                self.texts[path] = Path(path).read_text() if os.path.isfile(path) else ""
            text = re.sub(r"/\*.*?\*/|//[^\n]*", "", self.texts[path], flags=re.S)
            self.edges[path] = set()
            for name in _INCLUDE.findall(text):
                candidates = [os.path.join(root, name) for root in (os.path.dirname(path), *self.roots)]
                # Missing generated relative paths cannot shadow real authored
                # headers later in the compiler's include search order.
                target = next(
                    (
                        self.resolved(item)
                        for item in candidates
                        if os.path.isfile(item) or self.resolved(item) in self.supplied
                    ),
                    None,
                )
                if target is None:
                    target = next(
                        (self.resolved(item) for item in candidates if self.generated(self.resolved(item))), None
                    )
                if target is not None:
                    self.edges[path].add(target)
        return self.edges[path]

    def authored_frontier(self, path: str) -> set[str]:
        """Follow generated edges to authored inputs without claiming their names."""
        if path not in self.frontiers:
            pending, seen, authored = [path], set(), set()
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                if not self.generated(current):
                    authored.add(current)
                elif current in self.frontiers:
                    authored.update(self.frontiers[current])
                else:
                    pending.extend(self.imports(current))
            self.frontiers[path] = authored
        return self.frontiers[path]

    def paths(self, path: Path, *, authored_only: bool = False) -> set[Path]:
        pending, seen = [self.resolved(str(path))], set()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(
                    self.authored_frontier(current)
                    if authored_only and self.generated(current)
                    else self.imports(current)
                )
        return {Path(item) for item in seen}


def source_names(
    project: Project,
    header: Path,
    policy: Policy | None,
    *,
    consumers: dict[Path, set[str]] | None = None,
    texts: dict[Path, str] | None = None,
    consumer_tags: dict[Path, set[str]] | None = None,
) -> set[str]:
    """Reserve source names and collect each consumer's authored include ownership.

    Conditional source bodies must be viewed per version: simply deleting cpp
    directives can leave both arms' opening braces and hide later declarations.
    """
    import json

    from unbake.match.source_views import _preprocessed_lines, _version_lines
    from unbake.project.cache import Cache, key
    from unbake.typemap.declarations import clean
    from unbake.typemap.storage import generated

    supplied = texts
    closure = IncludeClosure(project, texts)
    cache = Cache(policy.cache_root if policy is not None else project.root / ".unbake/cache")
    header = Path(closure.resolved(str(header)))
    generator = key(Path(__file__))

    names: set[str] = set()
    header_owned: dict[Path, tuple[set[str], set[str]]] = {}
    sources = (
        (path for path in supplied if str(path).startswith(str(project.src) + os.sep) and path.suffix == ".c")
        if supplied is not None
        else project.src.rglob("*.c")
    )
    for path in sorted(sources):
        included = closure.paths(path, authored_only=True)
        if header not in included and not any(generated(project, dep) for dep in included):
            continue
        text = closure.texts[closure.resolved(str(path))]
        # Preserve the no-owned-type fast path, but still collect authored
        # header ownership for sources with no local typedefs or tags.
        views = {text} if re.search(r"\b(?:typedef|struct|union|enum)\b", declaration_source(text)) else set()
        if views and re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M):
            if policy is None:
                raise Held("solve", f"types.header_parse: {path}: policy.cpp required for conditional source names")
            lines = declaration_source(text).splitlines(keepends=True)
            views = set()
            for version in project.versions:
                active = _version_lines(project, policy, text, version)
                if active is None:
                    active = _preprocessed_lines(project, policy, text, version)
                views.add("".join(line for index, line in enumerate(lines) if index in active))
        owned: set[str] = set()
        local: set[str] = set()
        tags: set[str] = set()
        for view in views:
            content = clean(declaration_source(view))
            content_key = key(generator, content)

            def compute(output: Path, content: str = content, path: Path = path) -> None:
                parser = _Declarations(content)
                try:
                    row = parser.parse()
                except Held as error:
                    raise Held("solve", f"types.header_parse: {path}: {error.reason}") from error
                output.write_text(
                    json.dumps(
                        {
                            "names": sorted(parser.names),
                            "tags": sorted(row.tags),
                        }
                    )
                )

            row = json.loads(cache.produce("typemap-owned-names", content_key, compute).read_bytes())
            owned.update(row["names"])
            tags.update(row["tags"])
        names.update(owned)
        # Header ownership is per consumer. Reserving it globally would remove
        # aliases from consumers that never import that authored declaration.
        local.update(owned)
        for dep in sorted(included - {Path(closure.resolved(str(path)))}):
            if generated(project, dep):
                continue
            if dep not in header_owned:
                parser = _Declarations(clean(declaration_source(closure.texts[closure.resolved(str(dep))])))
                try:
                    row = parser.parse()
                except Held as error:
                    raise Held("solve", f"types.header_parse: {dep}: {error.reason}") from error
                header_owned[dep] = parser.names | parser.external_names, row.tags
            dep_names, dep_tags = header_owned[dep]
            local.update(dep_names)
            tags.update(dep_tags)
        if consumers is not None:
            consumers[path] = local
        if consumer_tags is not None:
            consumer_tags[path] = tags
    return names


def imports(project: Project, originals: dict[Path, str], filtered: dict[Path, str]) -> dict[Path, str]:
    """Render safe projections, including filtered transitive prerequisites.

    Inline only headers whose imported context changed, retaining their guards
    and translating relative include paths to the project's include roots.
    """
    paths = {path.resolve(): path for path in originals}
    edges: dict[Path, dict[str, Path]] = {}
    for path, text in originals.items():
        edges[path] = {}
        for name in _INCLUDE.findall(re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)):
            target = next(
                (
                    paths[item.resolve()]
                    for item in (path.parent / name, *(root / name for root in project.include))
                    if item.resolve() in paths
                ),
                None,
            )
            if target is not None:
                edges[path][name] = target
    changed = {path for path in originals if originals[path] != filtered[path]}
    while pending := {path for path, targets in edges.items() if changed.intersection(targets.values())} - changed:
        changed.update(pending)

    def relative(path: Path) -> str:
        return next(path.relative_to(root).as_posix() for root in project.include if path.is_relative_to(root))

    def render(path: Path, active: set[Path]) -> str:
        if path not in changed:
            return f'#include "{relative(path)}"'
        if path in active:
            return ""  # The containing guarded header is already being emitted.

        def include(match: re.Match[str]) -> str:
            target = edges[path].get(match[1])
            if target is None:
                return match[0]
            return render(target, active | {path})

        return _INCLUDE.sub(include, filtered[path])

    return {path: render(path, set()) for path in originals}
