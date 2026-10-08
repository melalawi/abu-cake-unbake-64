"""Common logical TU admission and durable editable result actions."""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path

from unbake import atomic
from unbake.config import Held, Project
from unbake.process import named


@dataclass(frozen=True)
class SourceScope:
    project: Path
    source: Path
    unit: str
    subject: str
    source_sha256: str
    include_roots: tuple[Path, ...]
    external_roots: tuple[Path, ...]

    def saved_action(
        self, saved: Path, *, exact: bool, config: Path | None = None, required_versions: tuple[str, ...] = ()
    ) -> str:
        words = ["unbake", "--project", str(self.project)]
        if config is not None:
            words.extend(("--config", str(config)))
        words.extend(("publish" if exact else "compare", str(saved)))
        for root in self.external_roots:
            words.extend(("--source-root", str(root)))
        for root in self.include_roots:
            if root != self.project / "include":
                words.extend(("--include-root", str(root)))
        for version in required_versions:
            words.extend(("--require-version", version))
        recipe = saved.with_suffix(".recipe.json")
        if recipe.is_file() and not exact:
            words.extend(("--recipe", str(recipe)))
        return shlex.join(words)


def admit_source(project: Project, source: Path, *, external_roots: tuple[Path, ...] = ()) -> SourceScope:
    from unbake.layout import split
    from unbake.work.compare import function_of

    source = source.resolve()
    subject = function_of(source)
    roots = tuple(dict.fromkeys(p.resolve() for p in (*project.admitted_roots, *external_roots)))
    if not source.is_relative_to(project.root) and not any(source.is_relative_to(root) for root in roots):
        raise Held(
            named(
                "source.external_root",
                f"{source}: explicitly admit its external root with --source-root DIR",
                owner="work.source_scope",
                stage="source",
            )
        )
    holders = [
        row for version in project.versions for row in split.functions(project, version) if subject in row.aliases
    ]
    paths = {f"src/{Path(row.path).as_posix()}.c" for row in holders}
    if len(paths) != 1:
        raise Held(
            named(
                "source.binding",
                f"{subject}: expected one actual translation-unit placement, found {sorted(paths)}",
                owner="work.source_scope",
                stage="source",
            )
        )
    unit = paths.pop()
    if source.is_relative_to(project.src) and source.relative_to(project.root).as_posix() != unit:
        raise Held(
            named(
                "source.binding",
                f"{source}: source differs from logical TU {unit}",
                owner="work.source_scope",
                stage="source",
            )
        )
    if source.is_relative_to(project.root) and not (
        source.is_relative_to(project.src) or source.is_relative_to(project.work)
    ):
        raise Held(
            named(
                "source.scope", "source must be inside src/ or build/work/", owner="work.source_scope", stage="source"
            )
        )
    used_includes = list(project.include)
    for include in project.include:
        if not include.resolve().is_relative_to(project.root) and not any(
            include.resolve().is_relative_to(root) for root in roots
        ):
            raise Held(
                named(
                    "source.include_root",
                    f"{include}: undeclared external include root",
                    owner="work.source_scope",
                    stage="source",
                )
            )
    from unbake.compilers import drivers

    for version in project.versions:
        options = [*drivers.resolved(project, version, unit).phase("preprocess"), *project.cppflags]
        pending = iter(options)
        for token in pending:
            path = None
            if token in ("-I", "-isystem", "-iquote", "-include", "-imacros"):
                value = next(pending, None)
                if value is None:
                    raise Held(
                        named(
                            "source.option",
                            f"{token}: missing include input",
                            owner="work.source_scope",
                            stage="source",
                        )
                    )
                path = Path(value)
            elif token.startswith("-I") and len(token) > 2:
                path = Path(token[2:])
            if path is not None:
                path = (path if path.is_absolute() else project.root / path).resolve()
                used_includes.append(path if token not in ("-include", "-imacros") else path.parent)
                if not path.is_relative_to(project.root) and not any(path.is_relative_to(root) for root in roots):
                    raise Held(
                        named(
                            "source.include_root",
                            f"{path}: recipe requires an explicitly admitted external root",
                            owner="work.source_scope",
                            stage="source",
                        )
                    )
    return SourceScope(
        project.root,
        source,
        unit,
        subject,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        tuple(dict.fromkeys(used_includes)),
        roots,
    )


def scoped_project(project: Project, source: Path, roots: tuple[Path, ...], include_roots: tuple[Path, ...]) -> Project:
    from dataclasses import replace

    declared = tuple(root.resolve() for root in roots)
    include = tuple(root.resolve() for root in include_roots)
    view = replace(
        project,
        work_include=tuple(dict.fromkeys((*project.work_include, *include, *declared))),
        admitted_roots=tuple(dict.fromkeys((*project.admitted_roots, *declared, *include))),
    )
    scope = admit_source(view, source, external_roots=tuple(dict.fromkeys((*declared, *include))))
    return replace(
        view, source_bindings=tuple(dict.fromkeys((*view.source_bindings, (str(source.resolve()), scope.unit))))
    )


def materialize(project: Project, scope: SourceScope, content: str) -> Path:
    digest = hashlib.sha256(content.encode()).hexdigest()
    path = project.work / "search" / scope.subject / digest / (scope.subject + ".c")
    if path.exists() and path.read_text() != content:
        raise Held(named("source.digest", "saved source digest collision", owner="work.source_scope", stage="source"))
    atomic.text(path, content)
    return path
