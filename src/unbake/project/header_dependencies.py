"""Close staged imports with exact bytes of missing, currently owned headers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from unbake.cdecl import declaration_source
from unbake.config import Held, Project
from unbake.layout import index
from unbake.process import named as cause_named
from unbake.project.headers import Graph, Include


class _OwnedBytes:
    """Read old render artifacts only as a source of manifest-pinned file bytes."""

    def __init__(self, project: Project, expected: dict[str, str]):
        self.project = project
        self.expected = expected
        self.payloads: dict[tuple[str, str], str] = {}
        self.artifacts = iter(sorted((project.cache / "typemap-render").glob("*/*")))

    def read(self, name: str, digest: str) -> str:
        key = (name, digest)
        relative = (self.project.include[-1] / name).relative_to(self.project.root).as_posix()
        if key in self.payloads:
            return self.payloads[key]
        for artifact in self.artifacts:
            if not re.fullmatch(r"[0-9a-f]{64}", artifact.name) or not artifact.is_file():
                continue
            try:
                value = json.loads(artifact.read_bytes())
            except (OSError, ValueError):
                continue
            outputs = value.get("outputs") if isinstance(value, dict) else None
            if not isinstance(outputs, dict):
                continue
            prefix = self.project.include[-1].relative_to(self.project.root).as_posix() + "/"
            for path, text in outputs.items():
                if isinstance(path, str) and path.startswith(prefix) and isinstance(text, str):
                    header = path[len(prefix) :]
                    digest_value = hashlib.sha256(text.encode()).hexdigest()
                    if self.expected.get(header) == digest_value:
                        self.payloads[header, digest_value] = text
            text = outputs.get(relative)
            if isinstance(text, str) and hashlib.sha256(text.encode()).hexdigest() == digest:
                return text
        raise Held(
            cause_named(
                "land.header_dependency",
                (
                    f"land.header_dependency: {name}: current manifest owns missing header, "
                    f"but its exact bytes are unavailable in the render cache; recompute headers"
                ),
                owner="project.header_dependencies",
                stage="land",
            )
        )


@dataclass(frozen=True)
class Closed:
    source: str
    headers: dict[str, str]


Signature = tuple[tuple[str, ...], tuple[str, ...]]


def _signature(text: str) -> Signature:
    """Compare entire declaration payloads, retaining non-guard directives.

    This is token equality, without typedef or structural equivalence. Different
    conditionals, macros and transitive imports cannot become the same payload.
    """
    directives = tuple(re.findall(r"^[ \t]*#[^\n]*", text, re.M))
    if len(directives) >= 3:
        guard = re.fullmatch(r"[ \t]*#[ \t]*ifndef[ \t]+(\w+)[ \t]*", directives[0])
        if (
            guard is not None
            and re.fullmatch(r"[ \t]*#[ \t]*define[ \t]+" + re.escape(guard[1]) + r"[ \t]*", directives[1])
            and re.fullmatch(r"[ \t]*#[ \t]*endif[ \t]*", directives[-1])
        ):
            directives = directives[2:-1]
    tokens = tuple(re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\w+|[^\s]', declaration_source(text)))
    return directives, tokens


def complete(project: Project, source: str, headers: dict[str, str]) -> Closed:
    """Close imports using exact current ownership and already installed homes.

    A cached artifact supplies only bytes pinned by the current manifest. When
    the full payload already exists in one installed manifest home, redirect the
    import to that home instead of installing a duplicate definition. Otherwise
    native proof can admit the missing owned bytes at their current listed home.
    """
    root = project.include[-1]
    lookup = index.load(project)["headers"]
    effective = dict(headers)
    origin = project.src / "_publication.c"
    graph = Graph.capture(project, {root / name: body for name, body in effective.items()})
    expected = Graph(
        graph.view.overlay({root / name: b"" for name in lookup if root / name not in graph.view.files}), graph.search
    )
    pending = [(origin, source)]
    seen: set[Path] = set()
    relocations: dict[str, str] = {}
    owned: _OwnedBytes | None = None
    signatures: dict[Signature, list[str]] | None = None
    while pending:
        parent, text = pending.pop()

        def replace_import(include: Include, original: str, parent: Path = parent) -> str:
            nonlocal owned, signatures
            name = include.name
            if include.unknown:
                raise Held(
                    cause_named(
                        "land.header_dependency",
                        f"land.header_dependency: {parent}: native dependency proof required for {name}",
                        owner="project.header_dependencies",
                        stage="land",
                    )
                )
            path = graph.resolve(parent, include).target or expected.resolve(parent, include).target
            if path is None:
                return original
            relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
            replacement = relocations.get(relative) if relative is not None else None
            if replacement is not None:
                path, relative = root / replacement, replacement
            if relative is not None and relative in effective:
                body = effective[relative]
            elif path.is_file():
                body = path.read_text()
            else:
                assert relative is not None
                if owned is None:
                    owned = _OwnedBytes(project, lookup)
                body = owned.read(relative, lookup[relative])
                if signatures is None:
                    signatures = {}
                    for candidate in sorted(lookup):
                        installed = root / candidate
                        if installed.is_file() and installed.resolve() == installed:
                            signature = _signature(effective.get(candidate, installed.read_text()))
                            signatures.setdefault(signature, []).append(candidate)
                homes = signatures.get(_signature(body), [])
                if len(homes) > 1:
                    raise Held(
                        cause_named(
                            "project.header_dependencies.replace_import",
                            f"land.header_home: {relative}: multiple installed homes have its full payload: "
                            + ", ".join(homes),
                            owner="project.header_dependencies",
                            stage="land",
                        )
                    )
                if homes:
                    replacement = homes[0]
                    relocations[relative] = replacement
                    path, relative = root / replacement, replacement
                    body = effective.get(relative, path.read_text())
                else:
                    effective[relative] = body
            if path not in seen:
                seen.add(path)
                pending.append((path, body))
            if replacement is not None:
                return original.replace(name, replacement, 1)
            return original

        rewritten = graph.rewrite_imports(text, replace_import)
        if rewritten != text:
            if parent == origin:
                source = rewritten
            elif parent.is_relative_to(root):
                effective[parent.relative_to(root).as_posix()] = rewritten
    return Closed(source, effective)
