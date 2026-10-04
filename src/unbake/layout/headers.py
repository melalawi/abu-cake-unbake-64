"""Dependency clusters placed at the lowest home covering transitive users."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unbake.decomp.draft_context import ordered_headers
from unbake.decomp.header_declarations import declaration_source, declarations
from unbake.layout.map import Group, Map
from unbake.project.config import Held
from unbake.typemap.header_names import alias_types
from unbake.typemap.split import guarded, required_providers

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)


def validate_edges(root: Path, edges: dict[Path, set[Path]], authored: set[Path]) -> None:
    def ancestor(parent: Path, child: Path) -> bool:
        if parent in authored:
            return True
        p, c = parent.relative_to(root), child.relative_to(root)
        return p == Path("common/types.h") or (p.name == "types.h" and p.parent == c.parent)

    active: set[Path] = set()
    done: set[Path] = set()

    def visit(path: Path) -> None:
        if path in active:
            raise Held("layout", f"layout.cycle: header include cycle at {path}")
        if path in done:
            return
        active.add(path)
        for dep in sorted(edges.get(path, set())):
            visit(dep)
        active.remove(path)
        done.add(path)

    for path in edges:
        visit(path)
    for path, deps in edges.items():
        for dep in deps:
            if not ancestor(dep, path):
                raise Held("layout", f"layout.includes: downward include {path} -> {dep}")


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
        ownership: Map,
        sources: dict[Path, str],
        declarations_by_name: dict[str, str] | None = None,
        symbol_segments: dict[str, str] | None = None,
        authored: set[Path] | None = None,
    ):
        self.aliases = {name: target for text in contents.values() for name, target in alias_types(text).items()}
        self.aliases.update(aliases or {})
        self.root = root
        self.render = render or (lambda path, text: guarded(path.relative_to(root), text))
        self.contents = contents
        self.parsed = {path: declarations(text) for path, text in contents.items()}
        evidence_exports: dict[Path, set[str]] = {}
        evidence_macros: dict[Path, dict[str, str]] = {}
        for path, text in contents.items():
            if "/* unbake declaration evidence:" not in text:
                continue
            macros = {
                match[1]: match[2]
                for match in re.finditer(
                    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(?:\([^\n]*?\))?[ \t]*((?:\\\n|[^\n])*)", text, re.M
                )
                if not match[1].startswith("UNBAKE_")
            }
            constants = {
                match[1]
                for body in re.findall(r"\benum\b[^{};]*\{([^{}]*)\}", declaration_source(text))
                for member in body.split(",")
                if (match := re.match(r"\s*([A-Za-z_]\w*)", member))
            }
            evidence_exports[path] = set(macros) | constants | self.parsed[path].declared
            evidence_macros[path] = macros
            self.aliases.update(macros)
        self.providers: dict[str, set[Path]] = {}
        self.tags: dict[str, set[Path]] = {}
        for path, row in self.parsed.items():
            for name in row.typedefs | row.exports | row.declared | evidence_exports.get(path, set()):
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
            if path not in (authored or set()) and ("{" in text or row.exports - row.tags or "(" in text):
                for name in re.findall(r"\b(?:struct|union|enum)\s+(\w+)", text):
                    if name not in row.tags:
                        deps.update(self.tags.get(name, set()))
            for name in _INCLUDE.findall(contents[path]):
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
            if path in evidence_macros:
                deps.update(
                    required_providers(
                        " ".join(evidence_macros[path].values()),
                        self.providers,
                        self.tags,
                        self.aliases,
                        evidence_exports[path] | row.typedefs,
                        row.tags,
                    )
                )
            deps.discard(path)
        self.ownership = ownership
        self.headers: dict[Path, bytes] = {}
        self.homes: dict[Path, Path] = {}
        self.groups = self._clusters()
        owners = ownership.owners
        by_group = {g.header: g for g in ownership.groups}
        by_group["common/types.h"] = Group("types", "common", "default", ())
        for segment in set((symbol_segments or {}).values()):
            by_group.setdefault(f"{segment}/types.h", Group("types", segment, "default", ()))
        users = {path: set[str]() for path in contents}
        source_providers: dict[Path, set[Path]] = {}
        for source, text in sources.items():
            owner = owners.get(source.stem)
            if owner is None:
                raise Held("layout", f"layout.member.{source.stem}: source has no group")
            for provider in required_providers(text, self.providers, self.tags, self.aliases):
                users[provider].add(owner.header)
                source_providers.setdefault(root / owner.header, set()).add(provider)
        for name, text in (declarations_by_name or {}).items():
            owner = owners.get(name)
            declaration_segment = (symbol_segments or {}).get(name)
            selected = (
                {owner.header}
                if owner is not None
                else ({f"{declaration_segment}/types.h"} if declaration_segment else {"common/types.h"})
            )
            for provider in required_providers(text, self.providers, self.tags, self.aliases):
                users[provider].update(selected)
        for path, row in self.parsed.items():
            users[path].update(owners[name].header for name in row.declared if name in owners)
        # First close actual users, then account for retained, unreachable
        # declarations living in common and close their prerequisites as well.
        for retained in (False, True):
            if retained:
                for path in users:
                    if not users[path] and path not in (authored or set()):
                        users[path].update(by_group)
            changed = True
            while changed:
                changed = False
                for path, deps in self.dependencies.items():
                    for dep in deps:
                        added = users[path] - users[dep]
                        if added:
                            users[dep].update(added)
                            changed = True
        authored = authored or set()
        for cluster in self.groups:
            used = set().union(*(users[p] for p in cluster))
            live_authored = cluster & authored
            if live_authored:
                if len(cluster) != 1:
                    raise Held(
                        "layout",
                        "layout.cycle: authored and generated declaration cycle: "
                        + ", ".join(str(p) for p in sorted(cluster)),
                    )
                destination = next(iter(live_authored))
            elif len(used) == 1:
                destination = root / by_group[next(iter(used))].header
            else:
                segments = {by_group[g].segment for g in used}
                destination = root / (next(iter(segments)) + "/types.h" if len(segments) == 1 else "common/types.h")
            for path in cluster:
                self.homes[path] = destination
        bodies: dict[Path, list[str]] = {root / "common/types.h": []}
        edges: dict[Path, set[Path]] = {}
        for group in ownership.groups:
            bodies.setdefault(root / group.header, [])
            bodies.setdefault(root / group.segment / "types.h", [])
            bodies.setdefault(root / group.segment / "data.h", [])
        for cluster in self.groups:
            destination = self.homes[next(iter(cluster))]
            if cluster & authored:
                continue
            dependencies = {self.homes[dep] for path in cluster for dep in self.dependencies[path]} - {destination}
            edges.setdefault(destination, set()).update(dependencies)
            tags = {
                m[0]
                for path in cluster
                for m in re.finditer(r"\b(?:struct|union)\s+[A-Za-z_]\w*", declaration_source(contents[path]))
            }
            lines = [tag + ";" for tag in sorted(tags)]
            for path in ordered_headers({p: contents[p] for p in sorted(cluster)}, aliases=self.aliases):
                lines.append(_INCLUDE.sub("", rendered[path]))
            bodies.setdefault(destination, []).extend(lines)
        self.symbols = {
            name: self.homes[sorted(providers)[0]].relative_to(root).as_posix()
            for name, providers in self.providers.items()
            if providers
        }
        for name, text in sorted((declarations_by_name or {}).items()):
            owner = owners.get(name)
            declaration_segment = (symbol_segments or {}).get(name)
            if owner is not None:
                destination = root / owner.header
            elif declaration_segment is not None:
                destination = root / declaration_segment / "data.h"
            else:
                destination = root / "common/types.h"
            bodies.setdefault(destination, []).append(text)
            self.symbols[name] = destination.relative_to(root).as_posix()
            deps = {self.homes[p] for p in required_providers(text, self.providers, self.tags, self.aliases)}
            edges.setdefault(destination, set()).update(deps - {destination})
        # A source can rely on a transitive authored type import even when no
        # synthesized prototype uses it. Its group retains that dependency.
        for destination, providers in source_providers.items():
            edges.setdefault(destination, set()).update(
                self.homes[provider] for provider in providers if self.homes[provider] != destination
            )
        # A declaration can force a wider home (e.g. data uses a private type).
        # Refuse a downward edge instead of manufacturing a cyclic include.
        validate_edges(root, edges, authored)
        for destination, lines in bodies.items():
            includes = [self.include(dep, destination) for dep in sorted(edges.get(destination, set()))]
            self.headers[destination] = self.render(destination, "\n".join(includes + lines))
        self.index: dict[str, Any] = {
            "schema": 1,
            "symbols": self.symbols,
            "type_headers": {
                name: sorted({self.homes[p].relative_to(root).as_posix() for p in paths})
                for name in self.providers.keys() | self.tags.keys()
                if (
                    paths := self.providers.get(name, set())
                    | self.tags.get(name, set())
                    | self.tags.get(self.aliases.get(name, "").removeprefix("struct ").removeprefix("union "), set())
                )
            },
            "clusters": {
                "\n".join(p.relative_to(root).as_posix() for p in sorted(cluster)): self.homes[next(iter(cluster))]
                .relative_to(root)
                .as_posix()
                for cluster in self.groups
                if not cluster & authored
            },
            "headers": {
                p.relative_to(root).as_posix(): hashlib.sha256(data).hexdigest() for p, data in self.headers.items()
            },
        }
        # Authored providers stay authored; their homes are not generated output.
        self.index["symbols"] = {name: home for name, home in self.symbols.items() if home in self.index["headers"]}
        self.index["type_headers"] = {
            name: [home for home in homes if home in self.index["headers"]]
            for name, homes in self.index["type_headers"].items()
        }

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

        for path in sorted(self.contents):
            if path not in index:
                visit(path)
        return groups

    def include(self, path: Path, destination: Path | None = None) -> str:
        from unbake.typemap.storage import relative_root

        name = relative_root(self.root, path)
        if (
            destination is not None
            and path.parent == self.root
            and path.name == "types.h"
            and destination.parent != self.root
        ):
            name = posixpath.relpath(path.as_posix(), destination.parent.as_posix())
        return f'#include "{name}"'

    def required(
        self, text: str, *, blocked: set[str] | None = None, blocked_tags: set[str] | None = None
    ) -> set[Path]:
        """Select body/signature type names, then let header includes close them."""
        selected = required_providers(text, self.providers, self.tags, self.aliases, blocked, blocked_tags)
        return {self.homes[path] for path in selected}
