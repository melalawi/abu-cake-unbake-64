"""Provision public compiler headers using family data and existing type providers."""

import re
from dataclasses import asdict
from pathlib import Path

from unbake import atomic, cache, inputs
from unbake.compilers.families.types import PublicHeader
from unbake.config import Held, Project
from unbake.process import named as cause_named

MARKER = "/* Generated public compiler provider. */"


def plan(project: Project) -> dict[Path, bytes]:
    from unbake.compilers.families import family_for
    from unbake.project.headers import include_headers
    from unbake.typemap.header_names import alias_types

    root = project.include[0]
    groups: dict[str, dict[str, PublicHeader]] = {}
    for compiler in project.compilers.values():
        for provider in family_for(compiler).public_headers():
            groups.setdefault(provider.name, {})[provider.selector] = provider
    changes = {}
    for name, variants in sorted(groups.items()):
        path = root / name
        if path.is_file() and not path.read_text().startswith(MARKER):
            # An installed SDK/human provider owns its API; provisioning never replaces it.
            continue
        declarations: dict[str, tuple[str, str]] = {}
        for provider in variants.values():
            for alias, expected, declaration in provider.aliases:
                if alias in declarations and declarations[alias] != (expected, declaration):
                    raise Held(
                        cause_named(
                            "compiler.header_alias",
                            f"compiler.header_alias: {name}: {alias}: incompatible families",
                            owner="compilers.headers",
                            stage="compiler-headers",
                        )
                    )
                declarations[alias] = expected, declaration
        owners: dict[str, str] = {}
        for header, relative in include_headers(project):
            if header == path.resolve():
                continue
            text = header.read_text()
            wanted = [
                alias
                for alias in declarations
                if re.search(r"\btypedef\b", text) and re.search(r"\b" + re.escape(alias) + r"\b", text)
            ]
            if not wanted:
                continue
            aliases = alias_types(text)
            for alias in wanted:
                if alias not in aliases:
                    continue
                if re.sub(r"\s+", "", aliases[alias]) != re.sub(r"\s+", "", declarations[alias][0]):
                    raise Held(
                        cause_named(
                            "compiler.header_alias",
                            f"compiler.header_alias: {relative}: {alias}: incompatible existing type",
                            owner="compilers.headers",
                            stage="compiler-headers",
                        )
                    )
                if alias in owners and owners[alias] != relative:
                    raise Held(
                        cause_named(
                            "compiler.header_alias",
                            f"compiler.header_alias: {alias}: multiple existing providers",
                            owner="compilers.headers",
                            stage="compiler-headers",
                        )
                    )
                owners[alias] = relative
        lines = [
            MARKER,
            "#ifndef UNBAKE_PUBLIC_" + re.sub(r"\W", "_", name).upper(),
            "#define UNBAKE_PUBLIC_" + re.sub(r"\W", "_", name).upper(),
        ]
        lines.extend(f"#include <{owner}>" for owner in sorted(set(owners.values())))
        lines.extend(declaration for alias, (_, declaration) in declarations.items() if alias not in owners)
        for index, (selector, provider) in enumerate(sorted(variants.items())):
            lines.append(("#if" if index == 0 else "#elif") + " defined(" + selector + ")")
            lines.append(provider.body)
        lines.extend(["#else", '#error "compiler public-header selector is missing"', "#endif", "#endif", ""])
        content = "\n".join(lines).encode()
        if not path.is_file() or path.read_bytes() != content:
            changes[path] = content
    return changes


def input_key(project: Project) -> str:
    from unbake.compilers.families import family_for
    from unbake.project.headers import include_headers

    providers = [
        (compiler.id, [asdict(p) for p in family_for(compiler).public_headers()])
        for compiler in project.compilers.values()
    ]
    managed = set(outputs(project))
    files = [
        asdict(inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured()))
        for path, _ in include_headers(project)
        if path not in managed or not path.read_text().startswith(MARKER)
    ]
    return cache.key("compiler-public-headers-v1", cache.serialized(providers), cache.serialized(files))


def outputs(project: Project) -> list[Path]:
    from unbake.compilers.families import family_for

    return sorted(
        {
            project.include[0] / header.name
            for compiler in project.compilers.values()
            for header in family_for(compiler).public_headers()
        }
    )


def run(project: Project) -> list[Path]:
    changes = plan(project)
    for path, content in changes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write(path, content)
    return sorted(changes)
