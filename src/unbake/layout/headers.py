"""Dependency clusters placed at the lowest home covering transitive users."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unbake.cdecl import declaration_source, declarations
from unbake.config import Held
from unbake.decomp.draft_context import ordered_headers
from unbake.layout.map import Group, Map
from unbake.typemap.header_names import alias_types
from unbake.typemap.split import guarded, required_providers

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', re.M)


SHARED_MIN_BYTES = 8192
"""A co-usage set whose declarations render smaller than this joins the nearest larger set."""


def shared_name(groups: frozenset[str]) -> str:
    """The shared type header for one set of using group headers."""
    return "common/types_" + hashlib.sha256("\n".join(sorted(groups)).encode()).hexdigest()[:12] + ".h"


def shared_header(name: str) -> bool:
    return re.fullmatch(r"common/types_[0-9a-f]{12}\.h", name) is not None


def strongly_connected(nodes: list[Any], edges: Callable[[Any], list[Any]]) -> list[set[Any]]:
    """Tarjan's components in a deterministic order (dependencies first)."""
    index: dict[Any, int] = {}
    low: dict[Any, int] = {}
    stack: list[Any] = []
    active: set[Any] = set()
    groups: list[set[Any]] = []

    def visit(node: Any) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        active.add(node)
        for dep in edges(node):
            if dep not in index:
                visit(dep)
                low[node] = min(low[node], low[dep])
            elif dep in active:
                low[node] = min(low[node], index[dep])
        if low[node] == index[node]:
            group = set()
            while True:
                member = stack.pop()
                active.remove(member)
                group.add(member)
                if member == node:
                    break
            groups.append(group)

    for node in nodes:
        if node not in index:
            visit(node)
    return groups


def co_usage(sizes: dict[frozenset[str], int], minimum: int) -> dict[frozenset[str], frozenset[str]]:
    """Map each user-group set to the set whose shared header holds it.

    A set below MINIMUM rendered bytes joins the kept set with the highest Jaccard overlap, then the smaller
    set, then the first name. With no set at the floor, the largest set is kept."""
    kept = sorted(key for key, size in sizes.items() if size >= minimum)
    if not kept and sizes:
        kept = [min(sizes, key=lambda key: (-sizes[key], sorted(key)))]
    result = {key: key for key in kept}
    for key in sorted(sizes.keys() - set(kept), key=sorted):
        result[key] = min(kept, key=lambda other: (-len(key & other) / len(key | other), len(other), sorted(other)))
    return result


def validate_edges(root: Path, edges: dict[Path, set[Path]], authored: set[Path]) -> None:
    def ancestor(parent: Path) -> bool:
        return parent in authored or shared_header(parent.relative_to(root).as_posix())

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
        if path.relative_to(root).as_posix() == "common/unused.h":
            continue
        for dep in deps:
            if not ancestor(dep):
                raise Held("layout", f"layout.includes: downward include {path} -> {dep}")


def one_home(parsed: dict[Path, Any], homes: dict[Path, Path], root: Path, authored: set[Path]) -> None:
    """Every generated type definition has one home: a typedef name or a struct/union body that two declaration
    components define is refused by name (the units that spell it must name it apart, never share it silently)."""
    definers: dict[tuple[str, str], list[Path]] = {}
    for path, row in parsed.items():
        if path in authored:
            continue
        for name in row.typedefs:
            definers.setdefault(("typedef", name), []).append(path)
        for name in row.tags:
            definers.setdefault(("struct", name), []).append(path)
    refused = []
    for (kind, name), paths in sorted(definers.items()):
        if len(paths) < 2:
            continue
        places = sorted({homes.get(path, path).relative_to(root).as_posix() for path in paths})
        where = " and ".join(places) if len(places) > 1 else f"{places[0]} twice"
        refused.append(f"{kind} {name} would be defined in {where}")
    if refused:
        raise Held("headers", "headers.type_home: " + "; ".join(refused))


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
        fixed_homes: dict[Path, set[Path]] | None = None,
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
            if not any(
                marker in text for marker in ("/* unbake declaration evidence:", "/* unbake published declaration:")
            ):
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
            # Declarator parsing intentionally omits extent expressions; their
            # macros/enum constants still have to precede retained declarations.
            for extent in re.findall(r"\[([^]]*)\]", declaration_source(contents[path])):
                expression_types.update(re.findall(r"\b[A-Za-z_]\w*\b", extent))
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
        authored = authored or set()
        # A published type keeps its recorded home only while that home is still a module header or an authored
        # header. A function or data declaration always lives in its owner's module header, whatever was recorded.
        current = {root / group.header for group in ownership.groups} | authored
        fixed = {
            path: homes & current
            for path, homes in (fixed_homes or {}).items()
            if homes & current and not self.parsed[path].declared & owners.keys()
        }
        users = {path: {home.relative_to(root).as_posix() for home in fixed.get(path, set())} for path in contents}
        source_providers: dict[Path, set[Path]] = {}
        self.local_names: dict[str, set[str]] = {}
        for source, text in sources.items():
            owner = owners.get(source.stem)
            if owner is None:
                raise Held("layout", f"layout.member.{source.stem}: source has no group")
            from unbake.layout import redeclarations

            local = set(redeclarations.declared(source, text))
            self.local_names.setdefault(owner.header, set()).update(local)
            local_types = set().union(
                *(
                    declarations(variant).typedefs
                    for start, end in redeclarations.spans(text)
                    for variant in redeclarations.variants(text[start:end])
                )
            )
            for provider in required_providers(
                text, self.providers, self.tags, self.aliases, local | local_types, redeclarations.local_tags(text)
            ):
                if provider not in fixed:
                    users[provider].add(owner.header)
                    source_providers.setdefault(root / owner.header, set()).add(provider)
        for name, text in (declarations_by_name or {}).items():
            selected = {self._declaration_home(name, owners, symbol_segments)}
            for provider in required_providers(text, self.providers, self.tags, self.aliases):
                users[provider].update(selected)
        for path, row in self.parsed.items():
            users[path].update(owners[name].header for name in row.declared if name in owners)
        # A dependency is used by every user of its dependents.
        changed = True
        while changed:
            changed = False
            for path, deps in self.dependencies.items():
                for dep in deps:
                    added = users[path] - users[dep]
                    if added:
                        users[dep].update(added)
                        changed = True
        # One home per cluster: an authored path, the module header owning a function or data declaration in it,
        # the single using header, the shared header of its co-usage set, or common/unused.h when nothing uses it.
        # Function and data declarations never move into a shared type header.
        shared: dict[int, frozenset[str]] = {}
        for number, cluster in enumerate(self.groups):
            used = set().union(*(users[p] for p in cluster))
            used.update(home.relative_to(root).as_posix() for path in cluster for home in fixed.get(path, set()))
            owning = {owners[name].header for path in cluster for name in self.parsed[path].declared if name in owners}
            live_authored = cluster & authored
            if len(owning) > 1 and not live_authored:
                raise Held(
                    "layout",
                    "layout.declaration_home: one declaration component declares symbols of "
                    + ", ".join(sorted(owning))
                    + ": "
                    + ", ".join(str(p) for p in sorted(cluster)),
                )
            if owning and not live_authored:
                used = owning
            if live_authored:
                if len(cluster) != 1:
                    raise Held(
                        "layout",
                        "layout.cycle: authored and generated declaration cycle: "
                        + ", ".join(str(p) for p in sorted(cluster)),
                    )
                destination = next(iter(live_authored))
            elif len(used) == 1:
                destination = root / next(iter(used))
            elif not used:
                destination = root / "common/unused.h"
            else:
                shared[number] = frozenset(used)
                continue
            for path in cluster:
                self.homes[path] = destination
        sizes: dict[frozenset[str], int] = {}
        for number, key in shared.items():
            sizes[key] = sizes.get(key, 0) + sum(len(rendered[path]) for path in self.groups[number])
        placement = co_usage(sizes, SHARED_MIN_BYTES)
        names = {number: shared_name(placement[key]) for number, key in shared.items()}
        # Merging sets can close an include cycle between shared headers; such headers become one.
        cluster_of = {path: number for number, cluster in enumerate(self.groups) for path in cluster}
        between: dict[str, set[str]] = {}
        for number, name in names.items():
            for path in self.groups[number]:
                for dep in self.dependencies[path]:
                    other = names.get(cluster_of[dep])
                    if other is not None and other != name:
                        between.setdefault(name, set()).add(other)
        for component in strongly_connected(sorted(set(names.values())), lambda n: sorted(between.get(n, ()))):
            first = min(component)
            names = {number: first if name in component else name for number, name in names.items()}
        for number, name in names.items():
            for path in self.groups[number]:
                self.homes[path] = root / name
        one_home(self.parsed, self.homes, root, authored)
        bodies: dict[Path, list[str]] = {}
        edges: dict[Path, set[Path]] = {}
        for group in ownership.groups:
            bodies.setdefault(root / group.header, [])
            bodies.setdefault(root / group.segment / "data.h", [])
        for cluster in self.groups:
            destination = self.homes[next(iter(cluster))]
            if cluster & authored:
                continue
            dependencies = {self.homes[dep] for path in cluster for dep in self.dependencies[path]} - {destination}
            edges.setdefault(destination, set()).update(dependencies)
            for path in cluster:
                for home in fixed.get(path, set()) - {destination}:
                    bodies.setdefault(home, [])
                    edges.setdefault(home, set()).add(destination)
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
            destination = root / self._declaration_home(name, owners, symbol_segments)
            bodies.setdefault(destination, []).append(text)
            self.symbols[name] = destination.relative_to(root).as_posix()
            deps = {self.homes[p] for p in required_providers(text, self.providers, self.tags, self.aliases)}
            edges.setdefault(destination, set()).update(deps - {destination})
        # A source can rely on a transitive authored type import even when no synthesized prototype uses
        # it; its group header retains that one. Generated homes are included by each source directly.
        for destination, providers in source_providers.items():
            edges.setdefault(destination, set()).update(
                self.homes[provider]
                for provider in providers
                if self.homes[provider] != destination and self.homes[provider] in authored
            )
        # A declaration can force a wider home (e.g. data uses a private type).
        # Refuse a downward edge instead of manufacturing a cyclic include.
        validate_edges(root, edges, authored)
        # Authored providers can rely on a scalar provider imported beside
        # them. Alphabetical order would put common views before types.h.
        home_edges: dict[Path, set[Path]] = {}
        for path, home in self.homes.items():
            home_edges.setdefault(home, set()).update(
                self.homes[dep] for dep in self.dependencies[path] if self.homes[dep] != home
            )
        components = strongly_connected(sorted(home_edges), lambda home: sorted(home_edges.get(home, set())))
        rank = {home: number for number, component in enumerate(components) for home in component}
        for destination, lines in bodies.items():
            imports = sorted(edges.get(destination, set()), key=lambda dep: (rank.get(dep, -1), dep))
            includes = [self.include(dep, destination) for dep in imports]
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
        return strongly_connected(sorted(self.contents), lambda path: sorted(self.dependencies[path]))

    def _declaration_home(self, name: str, owners: dict[str, Group], segments: dict[str, str] | None) -> str:
        """A prototype or extern lives in its owner's group header, else its segment's (or common) data.h.

        Every source includes its own group header, so a name that a source of the owner group declares itself
        lives in the segment's data.h, which only sources spelling the name import."""
        owner = owners.get(name)
        if owner is not None and name not in self.local_names.get(owner.header, set()):
            return owner.header
        segment = owner.segment if owner is not None else (segments or {}).get(name)
        return f"{segment}/data.h" if segment is not None else "common/data.h"

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
