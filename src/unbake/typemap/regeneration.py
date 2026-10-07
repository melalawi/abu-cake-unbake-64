"""Content-pinned header derivations at the project's cache boundary."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from unbake import atomic as atomic_files
from unbake import effort, inputs, pool, tui
from unbake.cache import Cache, key, memo
from unbake.config import Held, Host, Project
from unbake.layout import headers
from unbake.layout import index as layout_index
from unbake.layout import map as layout_map
from unbake.typemap import header_names, split, storage

# Bump when the value an artifact kind stores changes for the same inputs.
SOURCE_NAMES_SCHEMA = 6
RENDER_SCHEMA = 7


def artifact(cache: Cache, kind: str, content_key: str, compute: Callable[[], Any]) -> Any:
    def load() -> bytes:
        def make(output: Path) -> None:
            atomic_files.fresh(output, storage.encoded(compute()))

        return cache.produce(kind, content_key, make).read_bytes()

    # Retain immutable bytes, so mutations to a decoded result cannot poison reuse.
    content = memo("typemap-artifact." + kind, (str(cache.root), content_key), load, keep=32768)
    return json.loads(content)


class Certificates:
    """Immutable atomic batches replace one filesystem artifact per declaration.

    Concurrent publishers add independent batches; neither can overwrite the
    other's certificates. A failed validation never writes its pending batch.
    """

    def __init__(self, cache: Cache, environment: str) -> None:
        self.cache = cache
        self.directory = cache.root / "typemap-certificates" / environment
        self.known: set[str] | None = None

    def contains(self, content_key: str) -> bool:
        if self.known is None:
            self.known = set()
            for path in sorted(self.directory.glob("*.json")):
                self.known.update(json.loads(path.read_bytes()))
        return content_key in self.known

    def add(self, keys: set[str]) -> None:
        if not keys:
            return
        self.contains("")
        self.directory.mkdir(parents=True, exist_ok=True)
        content = storage.encoded(sorted(keys))
        storage.write(self.directory / (storage.digest(content) + ".json"), content, durable=False)
        if self.known is None:
            self.known = set()
        self.known.update(keys)


def environment(project: Project, policy: Host | None) -> str:
    """Pin generator code, configuration, version/compiler flags and actual tools."""
    code = Path(__file__).parents[1]
    sources = sorted(code.rglob("*.py"))
    tools: list[str | Path] = []
    if policy is not None:
        for field in ("cpp", "m2c"):
            path = Path(getattr(policy, field))
            tools.extend((field, path))
    for ident, compiler in sorted(project.compilers.items()):
        tools.extend((ident, compiler.cc, compiler.sha256))
    configuration = asdict(project)
    configuration.pop("units", None)
    configuration.pop("unit_flags", None)
    return key(
        "semantic-environment-v4",
        *(path for path in sources),
        json.dumps(storage.relocatable(configuration, project.root), default=str, sort_keys=True),
        *tools,
    )


def _projection(project: Project, policy: Host | None, path: Path, text: str, exported: set[str]) -> Any:
    from unbake.cdecl import declaration_source
    from unbake.typemap import declarations

    owned, tags = header_names._owned((project, policy, path, text))

    def tokens(view: str) -> list[str]:
        outside = declarations._unit_bodies_blanked(declaration_source(view))
        return [match[0] for match in declarations._C_TOKEN.finditer(outside)]

    try:
        declared: Any = tokens(text)
    except Held as error:
        if policy is None or not re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M):
            raise Held("solve", f"types.declaration: {path}: {error.reason}") from error
        from unbake.fold.source_views import active_source

        declared = {
            version: tokens(active_source(project, policy, text, version, path.stem)) for version in project.versions
        }
    return {
        "declarations": declared,
        "directives": re.findall(r"^[ \t]*#(?:\\\n|[^\n])*", text, re.M),
        "owned": owned,
        "tags": tags,
        "dependencies": sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", text)) & exported),
    }


@pool.cpu
def _projection_job(shared: Any, jobs: Any) -> None:
    project, policy, exported = shared
    cache = Cache(project.cache)
    for content_key, path, text in jobs:
        artifact(
            cache,
            "typemap-source-names",
            content_key,
            partial(_projection, project, policy, path, text, exported),
        )


class Session:
    def __init__(self, project: Project, policy: Host | None) -> None:
        self.project, self.policy = project, policy
        self.cache = Cache(project.cache)
        self.environment = environment(project, policy)
        self.certificates = Certificates(self.cache, self.environment)
        generated = storage.generated_view(project)
        self.authored = {
            path: path.read_text()
            for root in project.include
            for path in sorted(root.rglob("*.h"))
            if not generated(path)
        }
        self.sources = {path: path.read_text() for path in sorted(project.src.rglob("*.c"))}
        from unbake.typemap.declaration_evidence import published_snapshot

        self.published, self.published_homes = published_snapshot(project, sources=self.sources)
        self.ownership = layout_map.load(project)
        from unbake.cdecl import declarations as parsed_names
        from unbake.typemap import facts

        exported: set[str] = set()
        for path in {*self.authored, *layout_index.headers(project)}:
            row = parsed_names(self.authored.get(path) or path.read_text())
            exported.update(row.typedefs | row.exports | row.tags)
        exported_names = storage.encoded(sorted(exported))
        self.source_words = {path: set(re.findall(r"\b[A-Za-z_]\w*\b", text)) for path, text in self.sources.items()}
        self.projections: dict[Path, Any] = {}
        snapshot = facts.Snapshot(project)
        rows = []
        pending = []
        with tui.task("Selecting changed header consumers", len(self.sources)):
            for path, text in self.sources.items():
                bindings = storage.encoded(
                    {
                        version: facts._command(project, policy, version, path, marked=True)
                        for version in project.versions
                    }
                )
                content_key = key(
                    str(SOURCE_NAMES_SCHEMA),
                    self.environment,
                    storage.relative(project, path),
                    text,
                    exported_names,
                    bindings,
                    *(
                        facts.unit_key(project, policy, path, version, snapshot)
                        for version in project.versions
                        if re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M)
                    ),
                )
                rows.append((content_key, path, text))
                if self.cache.get("typemap-source-names", content_key) is None:
                    pending.append((content_key, path, text))
        if policy is not None:
            jobs = [pending[start : start + 32] for start in range(0, len(pending), 32)]
            with tui.task("Reading changed header consumers", len(pending)):
                pool.run(policy, _projection_job, jobs, (project, policy, exported))
        for content_key, path, text in rows:
            self.projections[path] = artifact(
                self.cache,
                "typemap-source-names",
                content_key,
                partial(_projection, project, policy, path, text, exported),
            )
        self.inputs = key(
            self.environment,
            layout_map.encoded(self.ownership),
            *(part for path, text in self.authored.items() for part in (storage.relative(project, path), text)),
            *(
                part
                for path, text in sorted(self.published.items())
                for part in (
                    storage.relative(project, path),
                    text,
                    storage.encoded(sorted(storage.relative(project, p) for p in self.published_homes[path])),
                )
            ),
            *(
                part
                for path, projection in self.projections.items()
                for part in (storage.relative(project, path), storage.encoded(projection))
            ),
        )
        self.reserved: set[str] = set()
        self.consumer_names: dict[Path, set[str]] = {}
        self._rewrite_contexts: dict[tuple[int, int], tuple[dict[str, str], frozenset[str], str, frozenset[str]]] = {}
        self.consumer_tags: dict[Path, set[str]] = {}

    def source_names(self, consumers: dict[Path, set[str]]) -> set[str]:
        def compute() -> Any:
            names = header_names.source_names(
                self.project,
                self.project.include[0] / "common/types.h",
                self.policy,
                consumers=consumers,
                texts={**self.authored, **self.sources},
                consumer_tags=self.consumer_tags,
            )
            return {
                "names": sorted(names),
                "consumers": {storage.relative(self.project, p): sorted(v) for p, v in consumers.items()},
                "tags": {storage.relative(self.project, p): sorted(v) for p, v in self.consumer_tags.items()},
            }

        value = artifact(self.cache, "typemap-source-names", key(str(SOURCE_NAMES_SCHEMA), self.inputs), compute)
        consumers.update({self.project.root / p: set(names) for p, names in value["consumers"].items()})
        self.consumer_tags.update({self.project.root / p: set(tags) for p, tags in value["tags"].items()})
        self.reserved = set(value["names"])
        return self.reserved

    def rewrite(self, text: str, replacements: dict[str, str], blocked: set[str]) -> str:
        index = id(replacements), id(blocked)
        context = self._rewrite_contexts.get(index)
        if context is None or context[0] != replacements or context[1] != blocked:
            frozen = frozenset(blocked)
            context_key = key(self.environment, storage.encoded(replacements), storage.encoded(sorted(frozen)))
            context = dict(replacements), frozen, context_key, frozenset(replacements) | frozen
            self._rewrite_contexts[index] = context
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", text)
        if context[3].isdisjoint(identifiers) and not any(map(header_names.placeholder, identifiers)):
            # No declaration token can change. The staged publication parser
            # still validates syntax, including declarations that need no rewrite.
            return text
        content_key = key(context[2], text)
        return str(
            artifact(
                self.cache, "typemap-rewrite", content_key, lambda: header_names.rewrite(text, replacements, blocked)
            )
        )

    def guarded(self, path: Path, text: str) -> bytes:
        content_key = key(self.environment, path.relative_to(self.project.include[0]).as_posix(), text)

        def make(output: Path) -> None:
            atomic_files.fresh(output, split.guarded(path.relative_to(self.project.include[0]), text))

        return memo(
            "typemap-guarded",
            (str(self.cache.root), content_key),
            lambda: self.cache.produce("typemap-header", content_key, make).read_bytes(),
            keep=32768,
        )

    def layout(
        self,
        contents: dict[Path, str],
        rendered: dict[Path, str],
        root: Path,
        aliases: dict[str, str],
        ownership: layout_map.Map,
        declarations_by_name: dict[str, str],
        symbol_segments: dict[str, str],
        fixed_homes: dict[Path, set[Path]],
    ) -> headers.Layout:
        return headers.Layout(
            contents,
            rendered,
            root,
            aliases=aliases,
            render=self.guarded,
            ownership=ownership,
            sources=self.sources,
            declarations_by_name=declarations_by_name,
            symbol_segments=symbol_segments,
            authored=set(self.authored),
            fixed_homes=fixed_homes,
        )

    _PROJECTED: ClassVar[dict[str, tuple[str, ...]]] = {
        "structs": (
            "state",
            "partial",
            "generated",
            "declaration",
            "aliases",
            "type",
            "typedefs",
            "reason",
            "common_base",
            "provenance",
        ),
        "functions": ("state", "prototype", "provenance"),
        "globals": ("state", "declaration", "provenance"),
        "arrays": ("state", "partial", "type", "provenance"),
    }
    _CARRIED = ("typedefs", "function_symbols", "declaration_evidence", "published_declarations", "published_homes")

    def _installed_headers(self) -> dict[str, str]:
        """Pin installed generated paths and actual bytes, including manual repairs."""
        names = layout_index.load(self.project)["headers"]
        paths = {self.project.include[0] / name for name in names}
        if not names and (self.project.root / "layout.toml").is_file():
            paths.update(layout_index.headers(self.project))
        return {storage.relative(self.project, path): inputs.digest(path) for path in sorted(paths) if path.is_file()}

    def _content_key(
        self, value: dict[str, Any], *, installed: dict[str, str] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """The render key: the session's inputs and the fields of the solution the render reads."""
        projection: dict[str, Any] = {
            kind: {name: {k: row[k] for k in keys if k in row} for name, row in value[kind].items()}
            for kind, keys in self._PROJECTED.items()
        }
        for name, row in value["functions"].items():
            projection["functions"][name]["caller_arguments"] = (row.get("abi") or {}).get("caller_arguments", [])
        projection.update((field, value.get(field, {})) for field in self._CARRIED)
        names = set().union(*(set(value[kind]) for kind in self._PROJECTED), set(value.get("typedefs", {})))
        projection["source_dependencies"] = {
            storage.relative(self.project, path): sorted(words & names) for path, words in self.source_words.items()
        }
        return key(
            str(RENDER_SCHEMA),
            self.inputs,
            storage.encoded(projection),
            storage.encoded(self._installed_headers() if installed is None else installed),
        ), projection

    def render(
        self, value: dict[str, Any], compute: Callable[[], dict[Path, bytes | Path]]
    ) -> dict[Path, bytes | Path]:
        """The rendered headers of VALUE, computed once per solution.

        Rendering records declaration evidence into VALUE, and VALUE is what the types step stores. The result is
        therefore kept under the stored solution's key too: the headers step, which renders the stored solution
        with the same inputs, gets the exact headers the types step validated and installed. A second render could
        differ from the first and change every generated header the source facts are keyed on."""
        installed = self._installed_headers()
        content_key, projection = self._content_key(value, installed=installed)
        state = self.cache.path("typemap-render-state", self.inputs)

        def delta() -> Any:
            # An index is the declaration lookup for cached views. Changing a
            # prototype requires rendering its entire group, not a leaf file.
            lookup = layout_index.load(self.project)
            if not state.is_file() or not lookup["headers"]:
                return None
            previous = json.loads(state.read_bytes())
            if previous.get("schema") != RENDER_SCHEMA or previous["projection"] != projection:
                return None
            for name, digest in lookup["headers"].items():
                path = self.project.include[0] / name
                if not path.is_file() or inputs.digest(path) != digest:
                    return None
            cached = self.cache.get("typemap-render", previous["content_key"])
            if cached is None:
                return None
            result = json.loads(cached.read_bytes())
            cached_headers = {
                name: storage.digest(text.encode())
                for name, text in result["outputs"].items()
                if Path(name).suffix == ".h"
            }
            # Validating the current index alone says nothing about cached
            # filenames: a repaired tree must not resurrect a previous home.
            return result if cached_headers == installed else None

        def make() -> Any:
            result = delta()
            if result is not None:
                return result
            effort.count("render.compute", 1, 1)
            outputs = compute()
            return {
                "outputs": {
                    storage.relative(self.project, p): data.decode()
                    for p, data in outputs.items()
                    if isinstance(data, bytes)
                },
                "declaration_headers": value["declaration_headers"],
                "shared_aliases": value["shared_aliases"],
                **{field: value.get(field, {}) for field in self._CARRIED},
                "reserved": sorted(self.reserved),
                "consumers": {
                    storage.relative(self.project, p): sorted(names) for p, names in self.consumer_names.items()
                },
                "consumer_tags": {
                    storage.relative(self.project, p): sorted(tags) for p, tags in self.consumer_tags.items()
                },
            }

        before_compute = effort.counted().get("render.compute", (0, 0))[0]
        result = artifact(self.cache, "typemap-render", content_key, make)
        computed = effort.counted().get("render.compute", (0, 0))[0] != before_compute
        effort.count("render.reused", int(not computed), 1)
        state_content = storage.encoded({"schema": RENDER_SCHEMA, "projection": projection, "content_key": content_key})
        if not state.is_file() or state.read_bytes() != state_content:
            storage.write(state, state_content, durable=False)
        value.update({field: result[field] for field in ("declaration_headers", "shared_aliases", *self._CARRIED)})
        self.reserved = set(result["reserved"])
        rendered_headers = {
            name: storage.digest(text.encode()) for name, text in result["outputs"].items() if Path(name).suffix == ".h"
        }
        # Reuse the validated render after installation, including the types
        # step's temporary retention of old homes until imports are rewritten.
        views = (installed, rendered_headers, {**installed, **rendered_headers})
        rendered = self.cache.path("typemap-render", content_key)

        def same(output: Path) -> None:
            atomic_files.copyfile(rendered, output, durable=False)

        for view in views:
            stored_key, _ = self._content_key(value, installed=view)
            if stored_key != content_key:
                self.cache.produce("typemap-render", stored_key, same)
        outputs = {self.project.root / p: data.encode() for p, data in result["outputs"].items()}
        effort.count(
            "render.unchanged_headers",
            sum(path.is_file() and path.read_bytes() == data for path, data in outputs.items()),
            len(outputs),
        )
        return outputs


def validation_inputs(
    project: Project, outputs: dict[Path, bytes | Path], abi_context: str, *, authored: dict[Path, str] | None = None
) -> tuple[dict[Path, bytes], dict[Path, set[Path]], list[tuple[str, set[Path]]]]:
    """Build one effective tree and select each header/ABI's include closure."""

    from unbake.cdecl import Declarations, declarations

    contents = (
        {path: text.encode() for path, text in authored.items()}
        if authored is not None
        else {
            path: path.read_bytes()
            for root in project.include
            for path in root.rglob("*.h")
            if not storage.generated(project, path)
        }
    )
    contents.update({p: content for p, content in outputs.items() if isinstance(content, bytes) and p.suffix == ".h"})
    graph_key = key(*(part for path, data in contents.items() for part in (str(path), data)))

    def graph() -> tuple[dict[Path, set[Path]], dict[str, set[Path]]]:
        paths = {str(path): path for path in contents}
        roots = tuple(map(str, project.include))
        edges: dict[Path, set[Path]] = {}
        for path, data in contents.items():
            edges[path] = set()
            for name in re.findall(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', data.decode(), re.M):
                for root in (os.path.dirname(str(path)), *roots):
                    target = paths.get(os.path.abspath(os.path.join(root, name)))
                    if target is not None:
                        edges[path].add(target)
                        break
        closures = {}
        for path in contents:
            seen: set[Path] = set()
            pending = [path]
            while pending:
                current = pending.pop()
                if current not in seen:
                    seen.add(current)
                    pending.extend(edges[current])
            closures[path] = seen
        providers: dict[str, set[Path]] = {}
        for path in sorted(contents, key=lambda p: not storage.generated(project, p)):
            data = contents[path]

            def analyze(data: bytes = data) -> Declarations:
                return declarations(data.decode())

            try:
                row = memo("typemap.validation-symbols", data, analyze, keep=32768)
            except Held as error:
                raise Held("m2c", f"{path}: {error.reason}") from error
            for name in row.typedefs | row.exports | row.tags:
                if storage.generated(project, path) or name not in providers:
                    providers.setdefault(name, set()).add(path)
        return closures, providers

    closures, providers = memo("typemap.validation-graph", graph_key, graph, keep=4)
    abi = []
    for text in dict.fromkeys(line for line in abi_context.splitlines() if line.strip()):
        selected = {p for name in re.findall(r"\b[A-Za-z_]\w*\b", text) for p in providers.get(name, set())}
        abi.append((text, {dep for p in selected for dep in closures.get(p, {p})}))
    return contents, closures, abi
