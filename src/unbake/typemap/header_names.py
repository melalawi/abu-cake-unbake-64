"""Keep source-owned names and draft placeholders out of shared declarations."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.header_declarations import Parser, declaration_source
from unbake.project.config import Held, Policy, Project

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\.\.\.|\S')
_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)
_PLACEHOLDER = re.compile(r"M2C_UNK\d*\Z")


def placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(name))


class _Declarations(Parser):
    def __init__(self, source: str, replacements: dict[str, str] | None = None, blocked: set[str] | None = None):
        super().__init__(source)
        self.matches = list(_TOKEN.finditer(declaration_source(source)))
        self.tokens = [match[0] for match in self.matches]
        self.source = source
        self.replacements = replacements or {}
        self.blocked = blocked or set()
        self.edits: list[tuple[int, int, str]] = []
        self.depth = 0
        self.names: set[str] = set()
        self.alias_types: dict[str, str] = {}

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


def source_names(project: Project, header: Path, policy: Policy | None) -> set[str]:
    """Reserve file-scope names in every C source importing HEADER transitively.

    Conditional source bodies must be viewed per version: simply deleting cpp
    directives can leave both arms' opening braces and hide later declarations.
    """
    from unbake.match.source_views import _preprocessed_lines, _version_lines
    from unbake.typemap.declarations import clean

    texts: dict[Path, str] = {}
    edges: dict[Path, set[Path]] = {}
    prototypes = (project.include[0] / "shared/prototypes.h").resolve()

    def imports(path: Path) -> set[Path]:
        path = path.resolve()
        if path not in edges:
            text = texts.setdefault(path, path.read_text() if path.is_file() else "")
            edges[path] = {header.resolve()} if path == prototypes else set()
            for name in _INCLUDE.findall(re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)):
                candidates = [path.parent / name, *(root / name for root in project.include)]
                target = next(
                    (
                        item.resolve()
                        for item in candidates
                        if item.is_file() or item.resolve() in (header.resolve(), prototypes)
                    ),
                    None,
                )
                if target is not None:
                    edges[path].add(target)
        return edges[path]

    def uses_header(path: Path) -> bool:
        pending, seen = [path.resolve()], set()
        while pending:
            current = pending.pop()
            if current == header.resolve():
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(imports(current))
        return False

    names: set[str] = set()
    for path in sorted(project.src.rglob("*.c")):
        if not uses_header(path):
            continue
        text = texts[path.resolve()]
        views = {text}
        if re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M):
            if policy is None:
                raise Held("solve", f"types.header_parse: {path}: policy.cpp required for conditional source names")
            lines = declaration_source(text).splitlines(keepends=True)
            views = set()
            for version in project.versions:
                active = _version_lines(project, policy, text, version)
                if active is None:
                    active = _preprocessed_lines(project, policy, text, version)
                views.add("".join(line for index, line in enumerate(lines) if index in active))
        for view in views:
            parser = _Declarations(clean(declaration_source(view)))
            try:
                parser.parse()
            except Held as error:
                raise Held("solve", f"types.header_parse: {path}: {error.reason}") from error
            names.update(parser.names)
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
