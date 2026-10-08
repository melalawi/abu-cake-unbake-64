"""Effective immutable source trees, declaration projections and include resolution."""

from __future__ import annotations

import os
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from unbake import cache, inputs
from unbake.config import Held
from unbake.process import named as cause_named

RECIPE_MODULES = (
    "project/headers.py",
    "cdecl.py",
    "typemap/split.py",
    "typemap/header_names.py",
    "layout/structs.py",
    "layout/structs_types.py",
    "layout/structs_identity.py",
)


def recipe() -> str:
    root = Path(__file__).parents[1]
    return cache.key(
        "graph-1",
        *(inputs.digest(root / name, algorithm="sha256", reuse=cache.configured()) for name in RECIPE_MODULES),
    )


def retention_recipe() -> str:
    root = Path(__file__).parents[1]
    return cache.key(
        recipe(),
        *(
            inputs.digest(root / name, algorithm="sha256", reuse=cache.configured())
            for name in ("layout/header_loss.py", "layout/header_step.py", "layout/redeclarations.py")
        ),
    )


@dataclass(frozen=True)
class FileBlob:
    path: Path
    signature: inputs.Signature | None
    _data: bytes | None = field(default=None, init=False, repr=False, compare=False)

    def read(self) -> bytes:
        signature = self.signature or inputs.signature(self.path)
        if inputs.signature(self.path) != signature:
            raise Held(
                cause_named(
                    "headers.changed",
                    f"headers.changed: {self.path}: immutable input changed",
                    owner="project.headers",
                    stage="headers",
                )
            )

        if self._data is not None:
            return self._data

        def read() -> bytes:
            data = self.path.read_bytes()
            if inputs.signature(self.path) != signature:
                raise Held(
                    cause_named(
                        "headers.changed",
                        f"headers.changed: {self.path}: input changed while reading",
                        owner="project.headers",
                        stage="headers",
                    )
                )
            return data

        data = cache.memo("tree.bytes", (str(self.path), signature), read, size=len, copy_out=bytes)
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "signature", signature)
        return data


class SearchFiles(Mapping[Path, bytes | FileBlob]):
    """A search observes only requested candidates, with immutable first-observation pins."""

    def __init__(
        self,
        supplied: Mapping[Path, bytes | FileBlob | None],
        roots: tuple[Path, ...],
        allowed: frozenset[Path] | None = None,
    ):
        self._observed = dict(supplied)
        self.roots = roots
        self.allowed = allowed

    def __iter__(self) -> Iterator[Path]:
        return (p for p, blob in self._observed.items() if blob is not None)

    def __len__(self) -> int:
        return sum(blob is not None for blob in self._observed.values())

    def __getitem__(self, path: Path) -> bytes | FileBlob:
        if path not in self._observed:
            if (self.allowed is None or path in self.allowed) and path.is_file():
                if not any(path.resolve().is_relative_to(root.resolve()) for root in self.roots):
                    raise Held(
                        cause_named(
                            "headers.symlink",
                            f"headers.symlink: {path}: forbidden include escape",
                            owner="project.headers",
                            stage="headers",
                        )
                    )
                target = path.resolve()
                self._observed[path] = (
                    self._observed[target]
                    if path.is_symlink() and target in self._observed
                    else FileBlob(path, inputs.signature(path))
                )
            else:
                self._observed[path] = None
        blob = self._observed[path]
        if blob is None:
            raise KeyError(path)
        return blob

    def overlay(self, changes: Mapping[Path, bytes | FileBlob | None]) -> SearchFiles:
        return SearchFiles({**self._observed, **changes}, self.roots, self.allowed)


@dataclass(frozen=True)
class TreeView:
    root_id: str
    files: Mapping[Path, bytes | FileBlob]
    roots: tuple[Path, ...]
    generated: frozenset[Path] = frozenset()
    root: Path = Path("/")
    cache_root: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.files, SearchFiles):
            object.__setattr__(
                self,
                "files",
                MappingProxyType({Path(os.path.abspath(path)): blob for path, blob in self.files.items()}),
            )
        object.__setattr__(self, "roots", tuple(Path(os.path.abspath(path)) for path in self.roots))

    def __reduce__(self) -> Any:
        return type(self), (self.root_id, dict(self.files), self.roots, self.generated, self.root, self.cache_root)

    @classmethod
    def capture(cls, project: Any, changes: Mapping[Path, bytes | str | Path | None] | None = None) -> TreeView:
        from unbake.layout import index

        paths = {p for root in project.include for p in root.rglob("*.h") if p.is_file()}
        paths.update(project.src.rglob("*.c"))
        paths.update(project.src.rglob("*.h"))
        generated = frozenset(index.headers(project))
        files = {p: FileBlob(p, inputs.signature(p)) for p in paths}
        view = cls(project.id, files, tuple(project.include), generated, project.root, project.cache)
        return view.overlay(changes or {})

    @classmethod
    def git(cls, project: Any, commit_paths: Iterable[str]) -> TreeView:
        from unbake import process

        def run(argv: list[str], stdin: str | None = None) -> str:
            return process.run_native(
                ["git", *argv], project.root, "land", temporary_root=project.build, stdin=stdin
            ).stdout

        revision = run(["rev-parse", "HEAD"]).strip()
        tracked = {p for p in run(["ls-tree", "-r", "-z", "--name-only", revision]).split("\0") if p}
        selected = set(commit_paths)
        files: dict[Path, bytes] = {}
        names = sorted(p for p in tracked - selected if Path(p).suffix in (".h", ".c"))
        if names:
            payload = run(["cat-file", "--batch"], "".join(f"{revision}:{p}\n" for p in names)).encode(
                "utf-8", "surrogateescape"
            )
            cursor = 0
            for name in names:
                end = payload.index(b"\n", cursor)
                header = payload[cursor:end].split()
                if len(header) != 3 or header[1] != b"blob":
                    raise Held(
                        cause_named(
                            "land.git_blob",
                            f"land.git_blob: {name}: exact blob required",
                            owner="project.headers",
                            stage="land",
                        )
                    )
                size = int(header[2])
                cursor = end + 1
                files[project.root / name] = payload[cursor : cursor + size]
                cursor += size + 1
        for name in selected:
            path = project.root / name
            if path.is_file() and path.suffix in (".h", ".c"):
                files[path] = path.read_bytes()
        return cls(project.id, files, tuple(project.include), root=project.root, cache_root=project.cache)

    @classmethod
    def contents(
        cls,
        files: Mapping[Path, str | bytes],
        roots: Iterable[Path],
        *,
        root: Path = Path("/"),
        cache_root: Path | None = None,
    ) -> TreeView:
        return cls(
            "projection",
            {p: b.encode() if isinstance(b, str) else b for p, b in files.items()},
            tuple(roots),
            root=root,
            cache_root=cache_root,
        )

    def overlay(self, changes: Mapping[Path, bytes | str | Path | None]) -> TreeView:
        files = dict(self.files)
        for path, blob in changes.items():
            path = Path(os.path.abspath(path))
            if blob is None:
                files.pop(path, None)
            else:
                files[path] = (
                    blob.read_bytes() if isinstance(blob, Path) else blob.encode() if isinstance(blob, str) else blob
                )
        return replace(self, files=files)

    def read(self, path: Path) -> bytes:
        blob = self.files[Path(os.path.abspath(path))]
        return blob.read() if isinstance(blob, FileBlob) else blob

    def logical(self, path: Path) -> inputs.LogicalPath:
        path = Path(os.path.abspath(path))
        if path.is_relative_to(self.root):
            return inputs.LogicalPath(self.root_id, path.relative_to(self.root).parts)
        for index, directory in enumerate(self.roots):
            if path.is_relative_to(directory):
                return inputs.LogicalPath("include" + str(index), path.relative_to(directory).parts)
        raise Held(
            cause_named(
                "headers.scope",
                "external header/source requires a declared search root",
                owner="project.headers",
                stage="headers",
                subject=path.name,
            )
        )

    def pin(self, path: inputs.LogicalPath) -> inputs.FilePin:
        if path.root == self.root_id:
            physical = self.root.joinpath(*path.parts)
        elif re.fullmatch(r"include[0-9]+", path.root) and int(path.root[7:]) < len(self.roots):
            physical = self.roots[int(path.root[7:])].joinpath(*path.parts)
        else:
            raise Held(
                cause_named(
                    "headers.scope", "logical input requires a declared root", owner="project.headers", stage="headers"
                )
            )
        blob = self.files.get(physical)
        if blob is None:
            return inputs.FilePin(path, "missing", None)
        if physical.is_symlink():
            target = physical.resolve()
            if not target.is_relative_to(self.root) and not any(target.is_relative_to(root) for root in self.roots):
                raise Held(
                    cause_named(
                        "headers.symlink",
                        f"headers.symlink: {physical}: forbidden escape",
                        owner="project.headers",
                        stage="headers",
                    )
                )
            return inputs.FilePin(
                path, "symlink", cache.key(os.readlink(physical), self.read(physical)), self.logical(target)
            )
        return inputs.FilePin(path, "file", inputs.bytes_digest(self.read(physical), algorithm="sha256"))


@dataclass(frozen=True)
class Search:
    quote_roots: tuple[Path, ...] = ()
    include_roots: tuple[Path, ...] = ()
    system_roots: tuple[Path, ...] = ()
    forced: tuple[str, ...] = ()
    macros: tuple[str, ...] = ()
    recipe: str = "graph-1"

    @classmethod
    def command(cls, root: Path, command: Iterable[str], defaults: tuple[Path, ...]) -> Search:
        flags = iter(command)
        quotes, ordinary, system, forced, macros = [], [], [], [], []
        for flag in flags:
            if flag in ("-I", "-iquote", "-isystem", "-include", "-imacros", "-D", "-U"):
                value = next(flags, "")
                if flag == "-I":
                    ordinary.append(root / value)
                elif flag == "-iquote":
                    quotes.append(root / value)
                elif flag == "-isystem":
                    system.append(root / value)
                elif flag in ("-include", "-imacros"):
                    forced.append(value)
                else:
                    macros.append(flag + value)
            elif flag.startswith("-I"):
                ordinary.append(root / flag[2:])
            elif flag.startswith(("-D", "-U")):
                macros.append(flag)
            elif flag == "in-memory":
                ordinary.extend(defaults)
        return cls(tuple(quotes), tuple(ordinary), tuple(system), tuple(forced), tuple(macros))


@dataclass(frozen=True)
class Include:
    name: str
    quoted: bool
    start: int
    end: int
    unknown: bool = False
    conditional: bool = False


def scan(text: str) -> tuple[Include, ...]:
    """One scanner preserves original spans through comments and logical line splices."""

    def compute() -> tuple[Include, ...]:
        splice_starts, removed, skipped = [], [], 0
        for match in re.finditer(r"\\\r?\n", text):
            splice_starts.append(match.start() - skipped)
            skipped += match.end() - match.start()
            removed.append(skipped)
        joined = re.sub(r"\\\r?\n", "", text) if skipped else text

        def offset(index: int) -> int:
            before = bisect_right(splice_starts, index)
            return index + (removed[before - 1] if before else 0)

        clean = re.sub(
            r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
            lambda m: re.sub(r"[^\n]", " ", m[0]) if m[0].startswith(("/*", "//")) else m[0],
            joined,
            flags=re.S,
        )
        guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\s*\n", clean)
        depth, result = 0, []
        for match in re.finditer(r"^[ \t]*#[ \t]*(\w+)\b([^\n]*)", clean, re.M):
            kind, body = match[1], match[2]
            if kind in ("if", "ifdef", "ifndef"):
                if not (guard and match.start() < guard.end()):
                    depth += 1
            elif kind == "endif":
                depth = max(0, depth - 1)
            elif kind == "include":
                literal = re.fullmatch(r'[ \t]*([<"])([^>"\n]+)[>"][ \t]*', body)
                result.append(
                    Include(
                        literal[2] if literal else body.strip(),
                        bool(literal and literal[1] == '"'),
                        offset(match.start()),
                        offset(match.end() - 1) + 1,
                        literal is None,
                        depth > 0,
                    )
                )
        return tuple(result)

    return cache.memo("graph.syntax", text, compute, size=cache.memory_size, copy_out=cache.clone)


def topology(text: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (line, body.strip())
        for line, body in enumerate(text.splitlines(), 1)
        if re.match(r"[ \t]*#[ \t]*(?:include|ifndef|define|endif)\b", body)
    )


@dataclass(frozen=True)
class DeclProjection:
    ordinary: frozenset[str]
    typedefs: frozenset[str]
    tags: frozenset[str]
    complete_tags: frozenset[str]
    macros: Mapping[str, str]
    statements: tuple[str, ...]
    uses: frozenset[str]
    complete_uses: frozenset[str]
    includes: tuple[Include, ...]
    parse_error: str | None
    declarations: Any = None

    @property
    def names(self) -> frozenset[str]:
        return self.ordinary | self.typedefs | frozenset(self.macros) | self.complete_tags


def project(text: str, reader_recipe: str) -> DeclProjection:
    from unbake.cdecl import declaration_source, declarations
    from unbake.typemap.split import statements

    def compute() -> DeclProjection:
        imports = scan(text)
        macros = {
            m[1]: m[2] for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(?:\([^\n]*?\))?[ \t]*(.*)", text, re.M)
        }
        try:
            row = declarations(text)
            guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\s*\n", text)
            end = re.search(r"^\s*#\s*endif[^\n]*\s*\Z", text, re.M)
            body = text[guard.end() : end.start()] if guard and end else text
            ordinary = set(row.declared)
            for enum in re.findall(r"\benum\b[^{};]*\{([^{}]*)\}", declaration_source(text)):
                ordinary.update(m[1] for member in enum.split(",") if (m := re.match(r"\s*([A-Za-z_]\w*)", member)))
            complete = frozenset(
                f"{kind} {name}"
                for kind, name in re.findall(r"\b(struct|union|enum)\s+(\w+)\s*\{", declaration_source(text))
            )
            return DeclProjection(
                frozenset(ordinary),
                frozenset(row.typedefs),
                frozenset(row.tags),
                complete,
                macros,
                tuple(statements(body)),
                frozenset(row.uses),
                frozenset(row.complete_uses),
                imports,
                None,
                row,
            )
        except Held as error:
            return DeclProjection(
                frozenset(),
                frozenset(),
                frozenset(),
                frozenset(),
                macros,
                (),
                frozenset(),
                frozenset(),
                imports,
                error.reason,
            )

    return cache.memo("graph.projection", (text, reader_recipe), compute, size=cache.memory_size, copy_out=cache.clone)


@dataclass(frozen=True)
class Resolution:
    target: Path | None
    probes: tuple[inputs.FilePin, ...]
    unknown: bool = False


@dataclass(frozen=True)
class Closure:
    paths: tuple[Path, ...]
    dependency_set: inputs.DependencySet
    unknown: bool
    order: tuple[Path, ...] = ()


class Graph:
    def __init__(self, view: TreeView, search: Search, *, project: Any = None) -> None:
        declared = tuple(dict.fromkeys((*view.roots, *search.quote_roots, *search.include_roots, *search.system_roots)))
        self.view, self.search, self.project = replace(view, roots=declared), search, project
        self._parsed: dict[Path, Any] = {}
        self._edges: dict[Path, tuple[Resolution, ...]] = {}
        self._projections: dict[Path, DeclProjection] = {}
        self._closures: dict[tuple[tuple[Path, ...], tuple[str, ...] | None, bool], Closure] = {}
        self._recipe = recipe()
        self._search_graphs: dict[Search, Graph] = {}
        self._lookups: dict[tuple[tuple[Path, ...], Search, tuple[Path, ...]], str] = {}

    @classmethod
    def capture(cls, project: Any, changes: Mapping[Path, bytes | str | Path | None] | None = None) -> Graph:
        return cls(TreeView.capture(project, changes), Search(include_roots=tuple(project.include)), project=project)

    @classmethod
    def contents(
        cls, contents: Mapping[Path, str | bytes], roots: Iterable[Path], *, cache_root: Path | None = None
    ) -> Graph:
        view = TreeView.contents(contents, roots, cache_root=cache_root)
        return cls(view, Search(include_roots=view.roots))

    def resolve(self, parent: Path, include: Include) -> Resolution:
        if include.unknown:
            return Resolution(None, (), True)
        roots = (
            *((parent.parent, *self.search.quote_roots) if include.quoted else ()),
            *self.search.include_roots,
            *self.search.system_roots,
        )
        probes = []
        for root in roots:
            candidate = Path(os.path.abspath(root / include.name))
            pin = self.view.pin(self.view.logical(candidate))
            probes.append(pin)
            if pin.state != "missing":
                return Resolution(candidate, tuple(probes), include.conditional)
        return Resolution(None, tuple(probes), include.conditional)

    def edges(self, path: Path) -> tuple[Resolution, ...]:
        if path not in self._edges:
            self._edges[path] = tuple(
                self.resolve(path, include) for include in scan(self.view.read(path).decode(errors="replace"))
            )
        return self._edges[path]

    def closure(
        self, roots: Iterable[Path], command: list[str] | None = None, *, authored_only: bool = False
    ) -> Closure:
        root_items = tuple(roots)
        identity = root_items, tuple(command) if command is not None else None, authored_only
        if identity in self._closures:
            return self._closures[identity]
        roots = root_items
        missing_roots = {
            Path(os.path.abspath(p)): FileBlob(Path(os.path.abspath(p)), inputs.signature(p))
            for p in roots
            if Path(os.path.abspath(p)) not in self.view.files and p.is_file()
        }
        if missing_roots:
            graph = Graph(
                replace(self.view, files={**self.view.files, **missing_roots}), self.search, project=self.project
            )
            result = graph.closure(roots, command, authored_only=authored_only)
            self._search_graphs.update(graph._search_graphs)
            self._closures[identity] = result
            return result
        if command is not None:
            search = Search.command(self.view.root, command, self.view.roots)
            files = dict(self.view.files)
            for root in (*search.quote_roots, *search.include_roots, *search.system_roots):
                for path in root.rglob("*.h"):
                    if path.is_file() and path not in files:
                        files[path] = FileBlob(path, inputs.signature(path))
            for name in search.forced:
                for root in (self.view.root, *search.quote_roots, *search.include_roots, *search.system_roots):
                    path = Path(os.path.abspath(root / name))
                    if path.is_file() and path not in files:
                        files[path] = FileBlob(path, inputs.signature(path))
            scope = replace(search, macros=())
            command_graph = self._search_graphs.get(scope)
            if command_graph is None:
                command_graph = Graph(replace(self.view, files=files), replace(search, macros=()), project=self.project)
                self._search_graphs[scope] = command_graph
            result = command_graph.closure(roots, authored_only=authored_only)
            self._closures[identity] = result
            return result
        roots = tuple(Path(os.path.abspath(p)) for p in roots)
        pending = list(reversed(roots))
        ordered: list[Path] = []
        seen: set[Path] = set()
        probes: dict[inputs.LogicalPath, inputs.FilePin] = {}
        unknown = False
        forced_parent = self.view.root / "_forced.c"
        for name in self.search.forced:
            resolution = self.resolve(forced_parent, Include(name, True, 0, 0))
            probes.update((pin.path, pin) for pin in resolution.probes)
            if resolution.target is not None:
                pending.append(resolution.target)
        while pending:
            path = pending.pop()
            if path in seen or path not in self.view.files:
                continue
            seen.add(path)
            ordered.append(path)
            pin = self.view.pin(self.view.logical(path))
            probes[pin.path] = pin
            for resolution in reversed(self.edges(path)):
                unknown |= resolution.unknown
                probes.update((pin.path, pin) for pin in resolution.probes)
                if resolution.target is not None:
                    pending.append(resolution.target)
        selected = seen - ({roots[0]} if roots and roots[0].suffix == ".c" else set())
        if authored_only:
            selected -= self.view.generated
        dependency_set = inputs.DependencySet(
            tuple(probes[p] for p in sorted(probes)),
            {
                "search": {
                    "quote_roots": [self.view.logical(p).name for p in self.search.quote_roots],
                    "include_roots": [self.view.logical(p).name for p in self.search.include_roots],
                    "system_roots": [self.view.logical(p).name for p in self.search.system_roots],
                    "forced": list(self.search.forced),
                    "macros": list(self.search.macros),
                    "recipe": self.search.recipe,
                },
                "roots": [self.view.logical(p).name for p in roots],
            },
            {"graph": self._recipe},
        )
        result = Closure(tuple(sorted(selected)), dependency_set, unknown, tuple(p for p in ordered if p in selected))
        self._closures[identity] = result
        return result

    def rewrite_imports(self, text: str, rewrite: Callable[[Include, str], str]) -> str:
        for include in reversed(scan(text)):
            original = text[include.start : include.end]
            replacement = rewrite(include, original)
            text = text[: include.start] + replacement + text[include.end :]
        return text

    def lookup_key(self, roots: Iterable[Path], command: list[str], relevant: Iterable[Path]) -> str:
        roots = tuple(roots)
        selected = set(relevant)
        scope = replace(Search.command(self.view.root, command, self.view.roots), macros=())
        identity = roots, scope, tuple(sorted(selected))
        if identity in self._lookups:
            return self._lookups[identity]
        closure = self.closure(roots, command)
        graph = self._search_graphs[scope]
        probes = {
            pin.path: pin
            for parent in (*tuple(roots), *closure.paths)
            if parent in graph.view.files
            for edge in graph.edges(parent)
            if edge.target in selected
            for pin in edge.probes
        }
        decisions = tuple(
            (p.path.name, p.state, p.link_target.name if p.link_target else None)
            for p in (probes[path] for path in sorted(probes))
        )
        result = cache.key(repr(decisions), repr(scope), str(closure.unknown), self._recipe)
        self._lookups[identity] = result
        return result

    def projection(self, path: Path) -> DeclProjection:
        if path not in self._projections:
            text = self.read(path).decode(errors="replace")
            content = cache.key(text, self._recipe, "C", "declarations")

            def compute() -> DeclProjection:
                return project(text, self._recipe)

            self._projections[path] = (
                cache.Cache(self.view.cache_root).value("header-projection", content, cache.PICKLE, compute)
                if self.view.cache_root is not None
                else compute()
            )
        return self._projections[path]

    def initialized_definitions(
        self, project: Any, source: Path, version: str, *, sizes: dict[str, int] | None = None
    ) -> dict[str, str]:
        """Current retained, version-active named initializers through the owning C parser."""
        import ast

        from pycparser import c_ast, c_generator  # type: ignore[import-untyped]

        from unbake import cdecl
        from unbake.fold import source_views
        from unbake.layout.structs_types import SCALARS
        from unbake.typemap import declarations
        from unbake.work import attempts

        if not source.is_relative_to(project.src) or source.suffix != ".c" or source not in self.view.files:
            return {}
        raw = self.read(source)
        if attempts.guard_present(raw.decode()):
            return {}
        closure = self.closure((source,))
        # Pin every literal branch provider. Conditional includes are complete
        # conservative inputs; a computed include still lacks source authority.
        if any(include.unknown for path in (source, *closure.paths) for include in scan(self.read(path).decode())):
            return {}
        typedefs = set()
        providers = []
        for path in closure.paths:
            if path != source:
                text = self.read(path).decode()
                typedefs.update(cdecl.declarations(text).typedefs)
                providers.append(text)
        cleaned = declarations.cleaned_unit(raw.decode())
        try:
            active = source_views.version_source(project, cleaned, version, source.stem)
        except Held as error:
            if error.key != "source.conditions":
                raise
            # Unresolved unrelated declarations cannot attest conditional
            # initializers. Preserve only unconditional file-scope statements.
            depth = 0
            lines = []
            for line in cleaned.splitlines(keepends=True):
                directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b", line)
                masked = depth > 0 or directive is not None
                if directive:
                    depth += 1 if directive[1] in {"if", "ifdef", "ifndef"} else -1 if directive[1] == "endif" else 0
                lines.append(re.sub(r"[^\n]", " ", line) if masked else line)
            active = "".join(lines)
        try:
            tree = cdecl.parser(typedefs).parse(declarations.cleaned_unit(active), filename=str(source))
        except cdecl.ParseError as error:
            raise Held(cause_named("data.definition", str(error), owner="project.headers", stage="data")) from error
        initializers = [
            node
            for node in tree.ext
            if isinstance(node, c_ast.Decl)
            and node.name
            and node.init is not None
            and "typedef" not in node.storage
            and "volatile" not in node.quals
        ]
        if not initializers:
            return {}
        generator = c_generator.CGenerator()
        aliases = {}
        needs_aliases = any(
            not isinstance(node.type, c_ast.ArrayDecl)
            or re.sub(r"\bconst\b\s*", "", declarations.node_type(node.type.type)).strip() != "char"
            for node in initializers
        )
        if sizes is not None and needs_aliases:
            for text in providers:
                # These are authored providers, not preprocessed translation
                # units. Hide complete logical directives before the type parse.
                try:
                    provider = cdecl.parser(typedefs).parse(declarations.cleaned_unit(cdecl.declaration_source(text)))
                except cdecl.ParseError as error:
                    raise Held(
                        cause_named("data.definition", str(error), owner="project.headers", stage="data")
                    ) from error
                aliases.update(
                    {
                        node.name: declarations.node_type(node.type)
                        for node in provider.ext
                        if isinstance(node, c_ast.Typedef)
                    }
                )
            aliases.update(
                {node.name: declarations.node_type(node.type) for node in tree.ext if isinstance(node, c_ast.Typedef)}
            )
        result = {}
        for node in initializers:
            result[node.name] = cache.key(
                self.view.logical(source).name,
                version,
                node.name,
                generator.visit(node),
                inputs.bytes_digest(raw, algorithm="sha256"),
                closure.dependency_set.digest,
            )
            if sizes is not None:
                spelling = declarations.canonical(declarations.node_type(node.type), aliases)
                spelling = re.sub(r"\b(?:const|volatile|restrict)\b\s*", "", spelling).strip()
                if spelling in SCALARS:
                    sizes[node.name] = SCALARS[spelling][0]
                elif (
                    isinstance(node.type, c_ast.ArrayDecl)
                    and node.type.dim is None
                    and isinstance(node.init, c_ast.Constant)
                    and node.init.type == "string"
                    and re.sub(r"\bconst\b\s*", "", declarations.node_type(node.type.type)).strip() == "char"
                ):
                    literal = ast.literal_eval(node.init.value)
                    if isinstance(literal, str) and literal.isascii():
                        sizes[node.name] = len(literal) + 1
        return result

    def included(self, source: Path, text: str) -> set[str]:
        graph = Graph(self.view.overlay({source: text}), self.search)
        return set().union(*(graph.projection(path).names for path in graph.closure((source,)).paths))

    def read(self, path: Path) -> bytes:
        path = Path(os.path.abspath(path))
        if path in self.view.files:
            return self.view.read(path)
        for graph in self._search_graphs.values():
            if path in graph.view.files:
                return graph.view.read(path)
        raise KeyError(path)

    def digest(self, path: Path) -> str:
        return inputs.bytes_digest(self.read(path), algorithm="sha256")

    def generated(self) -> frozenset[Path]:
        return self.view.generated

    def identifiers(self, path: Path) -> frozenset[str]:
        return frozenset(re.findall(r"[A-Za-z_]\w*", self.read(path).decode(errors="replace")))

    def parsed(self, path: Path) -> Any:
        from unbake.typemap.facts import parse

        if path not in self._parsed:
            self._parsed[path] = parse(self.read(path).decode())
        return self._parsed[path]

    def providers(self, names: Iterable[str], *, complete: bool = False) -> dict[str, set[Path]]:
        wanted = set(names)
        result: dict[str, set[Path]] = {}
        for path in self.view.files:
            if path.suffix != ".h":
                continue
            row = self.projection(path)
            if row.parse_error:
                raise Held(
                    cause_named(
                        "headers.projection",
                        f"headers.projection: {path}: {row.parse_error}",
                        owner="project.headers",
                        stage="headers",
                    )
                )
            offered = row.tags if complete else row.ordinary | row.typedefs | row.macros.keys()
            for name in wanted & set(offered):
                result.setdefault(name, set()).add(path)
        return result

    def affected(self, before: Graph, changed: Iterable[Path], consumers: Iterable[Path]) -> tuple[Path, ...]:
        changed = set(changed)
        names: set[str] = set()
        unknown = False
        for graph in (before, self):
            for path in changed & graph.view.files.keys():
                if path.suffix == ".h":
                    row = graph.projection(path)
                    names.update(row.names)
                    unknown |= row.parse_error is not None or any(i.unknown or i.conditional for i in row.includes)
        found = []
        for source in sorted(consumers):
            closures = [g.closure((source,)) for g in (before, self) if source in g.view.files]
            words = set().union(*(g.identifiers(source) for g in (before, self) if source in g.view.files))
            if (
                source in changed
                or unknown
                or any(c.unknown or changed & set(c.paths) for c in closures)
                or names & words
            ):
                found.append(source)
        return tuple(found)

    @classmethod
    def validation(
        cls,
        project: Any,
        outputs: Mapping[Path, bytes | Path],
        abi_context: str,
        *,
        authored: dict[Path, str] | None = None,
    ) -> tuple[dict[Path, bytes], dict[Path, set[Path]], list[tuple[str, set[Path]]]]:
        from unbake.typemap import storage

        is_generated = storage.generated_view(project)
        ownership: dict[Path, bool] = {}

        def generated(path: Path) -> bool:
            if path not in ownership:
                ownership[path] = is_generated(path)
            return ownership[path]

        contents = (
            {p: text.encode() for p, text in authored.items()}
            if authored is not None
            else {p: p.read_bytes() for root in project.include for p in root.rglob("*.h") if not generated(p)}
        )
        contents.update({p: data for p, data in outputs.items() if isinstance(data, bytes) and p.suffix == ".h"})
        categories = frozenset(p for p in contents if generated(p))
        view = replace(
            TreeView.contents(contents, project.include, root=project.root, cache_root=project.cache),
            generated=categories,
        )
        graph = cls(view, Search(include_roots=tuple(project.include)))
        closures = {p: set(graph.closure((p,)).paths) for p in contents}
        providers: dict[str, set[Path]] = {}
        for path in sorted(contents, key=lambda p: p not in view.generated):
            row = graph.projection(path)
            if row.parse_error:
                raise Held(cause_named(f"{path}", f"{path}: {row.parse_error}", owner="project.headers", stage="m2c"))
            for name in row.ordinary | row.typedefs | row.tags:
                if path in view.generated or name not in providers:
                    providers.setdefault(name, set()).add(path)
        abi = []
        for text in dict.fromkeys(line for line in abi_context.splitlines() if line.strip()):
            selected = {p for name in re.findall(r"\b[A-Za-z_]\w*\b", text) for p in providers.get(name, set())}
            abi.append((text, {dep for p in selected for dep in closures.get(p, {p})}))
        return contents, closures, abi

    def materialize(self, directory: Path) -> tuple[Path, ...]:
        from unbake import atomic

        written = []
        for path in self.view.files:
            target = directory.joinpath(*self.view.logical(path).parts)
            atomic.write(target, self.view.read(path))
            written.append(target)
        return tuple(written)


@dataclass(frozen=True)
class HeaderCheck:
    headers_checked: int
    headers_reused: int
    closures_checked: int
    closures_reused: int
    affected_consumers: tuple[Path, ...]
    dependency_set: inputs.DependencySet
    causes: tuple[str, ...] = ()
    outputs_digest: str = ""

    def valid(self, view: TreeView, outputs_digest: str) -> bool:
        return self.outputs_digest == outputs_digest and not inputs.changed(
            self.dependency_set,
            view,
            values={"inventory": sorted(view.logical(p).name for p in view.files)},
            recipes={"graph": recipe(), "retention": retention_recipe()},
        )


def include_headers(project: Any, *, exclude: Callable[[Path], bool] | None = None) -> list[tuple[Path, str]]:
    """(path, relative name) of every header; a file in a work include root hides the same name below it."""
    work_roots = tuple(getattr(project, "work_include", ()))
    seen_names: set[str] = set()
    seen: set[Path] = set()
    headers = []
    for root in project.include:
        root = Path(root)
        if exclude is None:
            paths = sorted(root.rglob("*.h"))
        else:
            paths = []
            for directory, folders, files in os.walk(root):
                parent = Path(directory)
                folders[:] = [name for name in folders if not exclude(parent / name)]
                paths.extend(parent / name for name in files if name.endswith(".h") and not exclude(parent / name))
            paths.sort()
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if root not in work_roots and relative in seen_names:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                seen_names.add(relative)
                headers.append((resolved, relative))
    return headers


class ProviderSet:
    """Installed homes for the same name closure used by the split generator."""

    def __init__(self, graph: Graph):
        from unbake.cdecl import declaration_source
        from unbake.typemap.header_names import alias_types

        contents = {path: graph.view.read(path).decode() for path in graph.view.files if path.suffix == ".h"}
        self.names: dict[str, set[Path]] = {}
        self.tags: dict[str, set[Path]] = {}
        self.macros: dict[str, str] = {}
        for path, text in contents.items():
            projection = graph.projection(path)
            if projection.parse_error:
                raise Held(
                    cause_named(
                        "headers.projection",
                        f"headers.projection: {path}: {projection.parse_error}",
                        owner="project.headers",
                        stage="headers",
                    )
                )
            row = projection.declarations
            macros = {name: body for name, body in projection.macros.items() if not name.startswith("UNBAKE_")}
            for name in row.typedefs | row.declared | macros.keys():
                self.names.setdefault(name, set()).add(path)
            for name in row.tags:
                self.tags.setdefault(name, set()).add(path)
            # Enum constants are exports too; the declaration reader skips their values.
            for enum in re.findall(r"\benum\b[^{};]*\{([^{}]*)\}", declaration_source(text)):
                for member in enum.split(","):
                    name = re.match(r"\s*([A-Za-z_]\w*)", member)
                    if name:
                        self.names.setdefault(name[1], set()).add(path)
            self.macros.update(alias_types(text))
            self.macros.update(macros)

        # Identical declarations can occur in broad compatibility headers and
        # smaller prerequisite headers; prefer the smallest complete provider.
        from unbake.layout import redeclarations

        sizes = {path: len(graph.projection(path).names) for path, text in contents.items()}
        catalogs = {path: redeclarations.catalog(text) for path, text in contents.items()}
        for name, paths in self.names.items():
            signatures = [catalogs[path].get(name, "") for path in paths]
            if (
                len(paths) > 1
                and all(signatures)
                and len({redeclarations.normalized(text) for text in signatures}) == 1
            ):
                chosen = min(paths, key=lambda path: (sizes[path], path.as_posix()))
                self.names[name] = {chosen}
                if name in self.tags:
                    self.tags[name] = {chosen}


def without_includes(source: str) -> str:
    """Mask known include spans without a second directive parser or changing source coordinates."""
    for directive in reversed(scan(source)):
        if not directive.unknown:
            span = source[directive.start : directive.end]
            source = source[: directive.start] + re.sub(r"[^\n]", " ", span) + source[directive.end :]
    return source
