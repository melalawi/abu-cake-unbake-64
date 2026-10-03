"""Content-pinned header derivations at the project's cache boundary."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake.project.cache import Cache, key, parsed, remembered
from unbake.project.config import Policy, Project
from unbake.typemap import header_names, split, storage


def artifact(cache: Cache, kind: str, content_key: str, compute: Callable[[], Any]) -> Any:
    def make(output: Path) -> None:
        output.write_bytes(storage.encoded(compute()))

    path = cache.produce(kind, content_key, make)
    return parsed(kind, path, lambda: json.loads(path.read_bytes()))


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
        content_key = key(self.environment, text, storage.encoded(replacements), storage.encoded(sorted(blocked)))
        return str(
            artifact(
                self.cache, "typemap-rewrite", content_key, lambda: header_names.rewrite(text, replacements, blocked)
            )
        )

    def guarded(self, path: Path, text: str) -> bytes:
        content_key = key(self.environment, path.name, text)

        def make(output: Path) -> None:
            output.write_bytes(split.guarded(path, text))

        return self.cache.produce("typemap-header", content_key, make).read_bytes()

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

        def make() -> Any:
            outputs = compute()
            return {
                "outputs": {str(p): data.decode() for p, data in outputs.items() if isinstance(data, bytes)},
                "declaration_headers": value["declaration_headers"],
                "shared_aliases": value["shared_aliases"],
                "reserved": sorted(self.reserved),
            }

        result = artifact(self.cache, "typemap-render", content_key, make)
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
        edges: dict[Path, set[Path]] = {}
        for path, data in contents.items():
            edges[path] = set()
            for name in re.findall(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', data.decode(), re.M):
                target = next(
                    (
                        p
                        for p in (path.parent / name, *(root / name for root in project.include))
                        if Path(os.path.abspath(p)) in contents
                    ),
                    None,
                )
                if target is not None:
                    edges[path].add(Path(os.path.abspath(target)))
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
