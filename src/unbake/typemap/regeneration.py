"""Content-pinned header derivations at the project's cache boundary."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake.project.cache import Cache, key, remembered
from unbake.project.config import Policy, Project
from unbake.typemap import header_names, split, storage


def artifact(cache: Cache, kind: str, content_key: str, compute: Callable[[], Any]) -> Any:
    def load() -> bytes:
        def make(output: Path) -> None:
            output.write_bytes(storage.encoded(compute()))

        return cache.produce(kind, content_key, make).read_bytes()

    # Retain immutable bytes, so mutations to a decoded result cannot poison reuse.
    content = remembered("typemap-artifact." + kind, (str(cache.root), content_key), load, keep=32768)
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
        storage.write(self.directory / (storage.digest(content) + ".json"), content)
        if self.known is None:
            self.known = set()
        self.known.update(keys)


def environment(project: Project, policy: Policy | None) -> str:
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
    def __init__(self, project: Project, policy: Policy | None) -> None:
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
        # A compatibility wrapper is both an authored input and a projected output.
        # Retain its original input keyed by the exact installed projection, so our
        # own narrowed imports do not trigger another render or accumulate whitespace.
        for path, text in self.authored.items():
            original = self.cache.get("typemap-authored", key(self.environment, str(path), text))
            if original is not None:
                self.authored[path] = original.read_text()
        self.sources = {path: path.read_text() for path in sorted(project.src.rglob("*.c"))}
        self.inputs = key(
            self.environment,
            *(part for path, text in {**self.authored, **self.sources}.items() for part in (str(path), text)),
        )
        self.reserved: set[str] = set()
        self.layout_value: dict[str, Any] | None = None
        self.consumer_names: dict[Path, set[str]] = {}
        self._rewrite_contexts: dict[tuple[int, int], tuple[dict[str, str], frozenset[str], str, frozenset[str]]] = {}

    def source_names(self, consumers: dict[Path, set[str]]) -> set[str]:
        def compute() -> Any:
            names = header_names.source_names(
                self.project,
                self.project.include[0] / "shared/typemap.h",
                self.policy,
                consumers=consumers,
                texts={**self.authored, **self.sources},
            )
            return {"names": sorted(names), "consumers": {str(p): sorted(v) for p, v in consumers.items()}}

        value = artifact(self.cache, "typemap-source-names", self.inputs, compute)
        consumers.update({Path(p): set(names) for p, names in value["consumers"].items()})
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
        content_key = key(self.environment, path.name, text)

        def make(output: Path) -> None:
            output.write_bytes(split.guarded(path, text))

        return remembered(
            "typemap-guarded",
            (str(self.cache.root), content_key),
            lambda: self.cache.produce("typemap-header", content_key, make).read_bytes(),
            keep=32768,
        )

    def layout(
        self, contents: dict[Path, str], rendered: dict[Path, str], root: Path, aliases: dict[str, str]
    ) -> split.Layout:
        content_key = key(
            self.environment,
            storage.encoded({str(p): t for p, t in contents.items()}),
            storage.encoded({str(p): t for p, t in rendered.items()}),
            storage.encoded(aliases),
        )

        def compute() -> Any:
            layout = split.Layout(contents, rendered, root, aliases=aliases, render=self.guarded)
            return {
                "aliases": layout.aliases,
                "headers": {str(p): data.decode() for p, data in layout.headers.items()},
                "homes": {str(p): str(home) for p, home in layout.homes.items()},
                "providers": {name: sorted(map(str, paths)) for name, paths in layout.providers.items()},
                "tags": {name: sorted(map(str, paths)) for name, paths in layout.tags.items()},
            }

        value = artifact(self.cache, "typemap-layout", content_key, compute)
        self.layout_value = value
        return self.restore_layout(value, root)

    def restore_layout(self, value: dict[str, Any], root: Path) -> split.Layout:
        layout = split.Layout.__new__(split.Layout)
        layout.root = root
        layout.render = self.guarded
        layout.aliases = value["aliases"]
        layout.headers = {Path(p): data.encode() for p, data in value["headers"].items()}
        layout.homes = {Path(p): Path(home) for p, home in value["homes"].items()}
        layout.providers = {name: set(map(Path, paths)) for name, paths in value["providers"].items()}
        layout.tags = {name: set(map(Path, paths)) for name, paths in value["tags"].items()}
        return layout

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
        projection = {
            kind: {name: {k: row[k] for k in keys if k in row} for name, row in value[kind].items()}
            for kind, keys in fields.items()
        }
        projection["typedefs"] = value.get("typedefs", {})
        # The legacy bridge is a render input; fresh projects do not acquire it.
        legacy = (self.project.include[0] / "shared/typemap.h").is_file() or any(
            re.search(r'#\s*include\s*"(?:(?:shared/)?typemap.h|shared/(?:types|consumers)/[^"]+)"', text)
            for text in self.authored.values()
        )
        content_key = key(self.inputs, storage.encoded(projection), str(legacy))

        state = self.cache.path("typemap-render-state", key(self.inputs, str(legacy)))

        def delta() -> Any:
            if not state.is_file():
                return None
            previous = json.loads(state.read_bytes())
            before = previous["projection"]
            if any(before[kind] != projection[kind] for kind in projection if kind != "functions"):
                return None
            if before["functions"].keys() != projection["functions"].keys():
                return None
            changed = [name for name, row in projection["functions"].items() if before["functions"][name] != row]
            if any(
                before["functions"][name]["state"] != "known" or projection["functions"][name]["state"] != "known"
                for name in changed
            ):
                return None
            path = self.cache.get("typemap-render", previous["content_key"])
            if path is None:
                return None
            result = json.loads(path.read_bytes())
            if result.get("layout") is None:
                return None
            layout = self.restore_layout(result["layout"], self.project.include[0])
            blocked = set(result["reserved"])
            for name in changed:
                record = value["functions"][name]
                text = self.rewrite(record["prototype"], result["shared_aliases"], blocked)
                if not text.startswith(("extern ", "static ")):
                    text = "extern " + text
                source = self.project.src / (name + ".c")
                selection = record["prototype"] + ("\n" + self.sources[source] if source in self.sources else "")
                homes = layout.required(selection, blocked=set(result["consumers"].get(str(source), [])))
                output = self.project.include[0] / "shared/decls" / (name + ".h")
                data = self.guarded(output, "\n".join(layout.include(home) for home in sorted(homes)) + "\n" + text)
                result["outputs"][str(output)] = data.decode()
            return result

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
                "layout": self.layout_value,
                "consumers": {str(p): sorted(names) for p, names in self.consumer_names.items()},
            }

        result = artifact(self.cache, "typemap-render", content_key, make)
        state_content = storage.encoded({"projection": projection, "content_key": content_key})
        if not state.is_file() or state.read_bytes() != state_content:
            storage.write(state, state_content)
        for name, text in result["outputs"].items():
            path = Path(name)
            if path in self.authored:

                def retain(output: Path, original: str = self.authored[path]) -> None:
                    output.write_text(original)

                self.cache.produce("typemap-authored", key(self.environment, name, text), retain)

        value.update({field: result[field] for field in ("declaration_headers", "shared_aliases")})
        self.reserved = set(result["reserved"])
        return {Path(p): data.encode() for p, data in result["outputs"].items()}


def validation_inputs(
    project: Project, outputs: dict[Path, bytes | Path], abi_context: str, *, authored: dict[Path, str] | None = None
) -> tuple[dict[Path, bytes], dict[Path, set[Path]], list[tuple[str, set[Path]]]]:
    """Build one effective tree and select each header/ABI's include closure."""

    from unbake.decomp.header_declarations import Declarations, declarations

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
    contents.update({p: content for p, content in outputs.items() if isinstance(content, bytes)})
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

            row = remembered("typemap.validation-symbols", data, analyze, keep=32768)
            for name in row.typedefs | row.exports | row.tags:
                if storage.generated(project, path) or name not in providers:
                    providers.setdefault(name, set()).add(path)
        return closures, providers

    closures, providers = remembered("typemap.validation-graph", graph_key, graph, keep=4)
    abi = []
    for text in dict.fromkeys(line for line in abi_context.splitlines() if line.strip()):
        selected = {p for name in re.findall(r"\b[A-Za-z_]\w*\b", text) for p in providers.get(name, set())}
        abi.append((text, {dep for p in selected for dep in closures.get(p, {p})}))
    return contents, closures, abi
