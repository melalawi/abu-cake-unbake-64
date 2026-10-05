"""Content-pinned header derivations at the project's cache boundary."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake import inputs
from unbake.cache import Cache, key, memo
from unbake.config import Host, Project
from unbake.layout import headers
from unbake.layout import index as layout_index
from unbake.layout import map as layout_map
from unbake.typemap import header_names, split, storage


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
    tools: list[str] = []
    if policy is not None:
        for field in ("cpp", "m2c"):
            path = getattr(policy, field, None)
            if path and Path(path).is_file():
                tools.extend((str(path), key(Path(path))))
    config = project.root / "config.toml"
    return key(
        *(path for path in sources),
        config,
        json.dumps(asdict(project), default=str, sort_keys=True),
        json.dumps(vars(policy) if policy is not None else None, default=str, sort_keys=True),
        *tools,
    )


class Session:
    def __init__(self, project: Project, policy: Host | None) -> None:
        self.project, self.policy = project, policy
        self.cache = Cache(policy.cache_root if policy is not None else project.root / ".unbake/cache")
        self.environment = environment(project, policy)
        self.certificates = Certificates(self.cache, self.environment)
        self.authored = {
            path: path.read_text()
            for root in project.include
            for path in sorted(root.rglob("*.h"))
            if not storage.generated(project, path)
        }
        self.sources = {path: path.read_text() for path in sorted(project.src.rglob("*.c"))}
        self.ownership = layout_map.load(project)
        self.inputs = key(
            self.environment,
            layout_map.encoded(self.ownership),
            *(part for path, text in {**self.authored, **self.sources}.items() for part in (str(path), text)),
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
                "consumers": {str(p): sorted(v) for p, v in consumers.items()},
                "tags": {str(p): sorted(v) for p, v in self.consumer_tags.items()},
            }

        value = artifact(self.cache, "typemap-source-names", self.inputs, compute)
        consumers.update({Path(p): set(names) for p, names in value["consumers"].items()})
        self.consumer_tags.update({Path(p): set(tags) for p, tags in value["tags"].items()})
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

    def render(
        self, value: dict[str, Any], compute: Callable[[], dict[Path, bytes | Path]]
    ) -> dict[Path, bytes | Path]:
        fields = {
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
            ),
            "functions": ("state", "prototype"),
            "globals": ("state", "declaration"),
            "arrays": ("state", "partial", "type"),
        }
        projection: dict[str, Any] = {
            kind: {name: {k: row[k] for k in keys if k in row} for name, row in value[kind].items()}
            for kind, keys in fields.items()
        }
        projection["typedefs"] = value.get("typedefs", {})
        projection["declaration_evidence"] = value.get("declaration_evidence", {})
        projection["published_declarations"] = value.get("published_declarations", {})
        projection["published_homes"] = value.get("published_homes", {})
        content_key = key(self.inputs, storage.encoded(projection))
        state = self.cache.path("typemap-render-state", self.inputs)

        def delta() -> Any:
            # An index is the declaration lookup for cached views. Changing a
            # prototype requires rendering its entire group, not a leaf file.
            lookup = layout_index.load(self.project)
            if not state.is_file() or not lookup["headers"]:
                return None
            previous = json.loads(state.read_bytes())
            if previous["projection"] != projection:
                return None
            for name, digest in lookup["headers"].items():
                path = self.project.include[0] / name
                if not path.is_file() or inputs.digest(path) != digest:
                    return None
            cached = self.cache.get("typemap-render", previous["content_key"])
            return json.loads(cached.read_bytes()) if cached is not None else None

        def make() -> Any:
            result = delta()
            if result is not None:
                return result
            outputs = compute()
            return {
                "outputs": {str(p): data.decode() for p, data in outputs.items() if isinstance(data, bytes)},
                "declaration_headers": value["declaration_headers"],
                "shared_aliases": value["shared_aliases"],
                "reserved": sorted(self.reserved),
                "consumers": {str(p): sorted(names) for p, names in self.consumer_names.items()},
                "consumer_tags": {str(p): sorted(tags) for p, tags in self.consumer_tags.items()},
            }

        result = artifact(self.cache, "typemap-render", content_key, make)
        state_content = storage.encoded({"projection": projection, "content_key": content_key})
        if not state.is_file() or state.read_bytes() != state_content:
            storage.write(state, state_content, durable=False)
        value.update({field: result[field] for field in ("declaration_headers", "shared_aliases")})
        self.reserved = set(result["reserved"])
        return {Path(p): data.encode() for p, data in result["outputs"].items()}


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

            row = memo("typemap.validation-symbols", data, analyze, keep=32768)
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
