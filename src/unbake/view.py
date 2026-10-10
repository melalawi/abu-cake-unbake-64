"""Preprocessed source views pinned to active project dependencies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from unbake import adapters, effort, pool, store
from unbake import config as configuration
from unbake.contracts import Finding, Recipe, Refusal, Snapshot, SourceView, UnitSpec, digest

_CODE = digest(Path(__file__).read_bytes())  # a view is only as true as the dependency rules that pinned it

def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
def _pin(snapshot: Snapshot, path: str) -> str:
    """The hash of a project file as the snapshot reads it; a file on disk is hashed once per command, until
    store.write replaces one."""
    try:
        if path in snapshot.overlays:
            return _sha(snapshot.read(path))
        full = f"{snapshot.config.project.root}/{path}"
        return cast(str, effort.memo(("pin", full), lambda: _sha(Path(full).read_bytes())))
    except FileNotFoundError:
        return "missing"
def _path(marker: str, root: Path, overlay: Path | None, real: bool = True) -> tuple[str, bool]:
    if marker.startswith("<") and marker.endswith(">"):
        return marker, False
    path = Path(marker)
    path = path if path.is_absolute() else root / path
    path = path.resolve() if real else Path(os.path.normpath(path))  # real follows links; a closure only needs names
    for base in (overlay, root):
        if base is not None and path.is_relative_to(base):
            return path.relative_to(base).as_posix(), True
    return str(path), False
_DIRECTIVE = re.compile(rb'^[ \t]*(?:#[ \t]*include|\.include)[ \t]*([<"])([^>"\n]+)[>"]', re.M)
def _directives(content: bytes) -> tuple[tuple[bytes, bytes], ...]:
    return tuple(_DIRECTIVE.findall(content))
def _link_relative(overlay: Path, root: Path, path: str, content: bytes) -> None:
    """A quoted include is found beside the file naming it: link such files of the real tree into the overlay."""
    for _, name in _directives(content):
        relative = os.path.normpath(Path(path).parent / name.decode())
        real, target = root / relative, overlay / relative
        if not relative.startswith("..") and real.is_file() and not (target.exists() or target.is_symlink()):
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
            pending.unlink(missing_ok=True)
            pending.symlink_to(real)
            os.replace(pending, target)  # atomic: another job making the same link finds it whole, never half-made
            _link_relative(overlay, root, relative, real.read_bytes())
def headers(snapshot: Snapshot, unit: UnitSpec, argv: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the project files the unit can include, the paths a quoted or forced name found nowhere would be read from)
    under the search flags of the command that reads the source (-iquote, -I, -isystem, -include, -imacros), read
    through the snapshot so a planned file counts too. An unfound <name> is the toolchain's own; an unfound quoted
    name is the caller's to cover, never dropped here."""
    def text(path: str) -> tuple[tuple[bytes, bytes], ...] | None:
        """What one file includes, read and parsed once per snapshot."""
        def read() -> tuple[tuple[bytes, bytes], ...] | None:
            content = snapshot.peek(path)
            return None if content is None else _directives(content)
        return cast("tuple[tuple[bytes, bytes], ...] | None", effort.memo(("includes", snapshot.digest, path), read))
    dirs: dict[str, list[str]] = {"-iquote": [], "-I": [], "-isystem": []}
    forced: list[str] = []
    words = iter(argv)
    for word in words:
        flag = next((f for f in ("-include", "-imacros", "-iquote", "-isystem", "-I") if word.startswith(f)), None)
        if flag is None or word == "-I-":
            continue
        value = word[len(flag):] or next(words, "")  # each of these flags takes the next word when bare
        if value:
            (forced if flag in ("-include", "-imacros") else dirs[flag]).append(value)
    angle = (*dirs["-I"], *dirs["-isystem"])
    found: set[str] = set()
    missing: set[str] = set()
    pending = [unit.path]
    def reach(name: str, bases: Sequence[str], quoted: bool) -> None:
        options = [os.path.normpath(os.path.join(base, name)) for base in bases]
        candidate = next((c for c in options if text(c) is not None), None)
        if candidate is None:
            missing.update(options if quoted else ())
        elif candidate not in found:
            found.add(candidate)
            pending.append(candidate)
    for name in forced:  # searched first in the working directory, then as a quoted include of the source
        reach(name, (".", os.path.dirname(unit.path), *dirs["-iquote"], *angle), True)
    while pending:
        path = pending.pop()
        for delimiter, raw in text(path) or ():
            quoted = delimiter == b'"'
            reach(raw.decode(), (os.path.dirname(path), *dirs["-iquote"], *angle) if quoted else angle, quoted)
    return tuple(sorted(found - {unit.path})), tuple(sorted(missing))
def closure(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[tuple[str, str], ...] | None:
    """(path, pin) of the unit's source and every project file it can reach. None when an overlay holds one of them."""
    return effort.memo(("closure", snapshot.digest, unit.path, version), lambda: _closure(snapshot, unit, version))
def _search(snapshot: Snapshot, version: str) -> list[str]:
    cfg = snapshot.config
    macros = configuration.load_resource("repo.toml")["splat"]["options"]["generated_asm_macros_directory"]
    return ["-Iinclude", "-Isrc", "-I" + macros.format(version=version, name=cfg.project.name)]
def _closure(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[tuple[str, str], ...] | None:
    names = [unit.path, *headers(snapshot, unit, _search(snapshot, version))[0]]
    return None if snapshot.overlays.keys() & set(names) else tuple((n, _pin(snapshot, n)) for n in names)
def _lines(text: str, root: Path, overlay: Path | None, source: str):
    lines, files = [], {source}
    current, number = source, 1
    for output, text_line in enumerate(text.splitlines(), 1):
        marker = text_line.startswith("# ") and re.match(r'^# (\d+) "([^"]+)"', text_line)
        if marker:
            current, project_file = _path(marker[2], root, overlay)
            number = int(marker[1])
            if project_file:
                files.add(current)
        else:
            lines.append((output, current, number))
            number += 1
    return tuple(lines), [source, *sorted(files - {source})]
def get(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, *, lines: bool = True) -> SourceView:
    """The preprocessed view; lines=False skips the per-line marker table that only active_lines needs."""
    with effort.stage("view.get"):
        config = snapshot.config
        root = config.project.root.resolve()
        overlay = root / "build" / "views" / snapshot.digest[:16] if snapshot.overlays else None
        try:
            source_hash = _sha(snapshot.read(unit.path))
        except FileNotFoundError:
            raise Refusal(Finding("preprocess.error", "Source file is missing.", path=unit.path)) from None
        # every file the build can reach, pinned; an overlay holding one makes the snapshot itself the pin
        reads = closure(snapshot, unit, version)
        key = digest((unit.path, source_hash, recipe.digest, version, config.project.version_macros[version], _CODE,
                      snapshot.digest if reads is None else reads))
        def produce() -> bytes:
            if overlay is not None:
                for path, content in snapshot.overlays.items():
                    target = overlay / path
                    if content is not None and not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                    if content is not None:
                        _link_relative(overlay, root, path, content)
            include = [root / "include", root / "src"]
            if overlay is not None:
                include = [overlay / "include", include[0], overlay / "src", include[1]]
            source = (overlay if overlay is not None and unit.path in snapshot.overlays else root) / unit.path
            work = root / "build" / "views" / snapshot.digest[:16] / ".work" / key
            work.mkdir(parents=True, exist_ok=True)
            out = work / "view.i"
            toolchain = adapters.toolchain(config, recipe.toolchain)
            # -P drops the line markers
            marked = replace(recipe, cppflags=tuple(f for f in recipe.cppflags if f != "-P"))
            result = toolchain.preprocess(source, marked, version, include, out=out)
            findings = list(toolchain.diagnose(result))
            stderr = result.stderr.decode(errors="replace")
            if not findings and (result.exit != 0 or result.signal is not None):
                findings.append(Finding("preprocess.error", stderr or "Preprocessing failed.", path=unit.path))
            missing = re.search(r'(\S+): No such file or directory|Cannot open include file "(\S+)"', stderr)
            if missing and (findings or result.exit != 0 or result.signal is not None):
                header = missing[1] or missing[2]
                findings.append(Finding("view.include", f"Include file {header} is missing.",
                                        path=unit.path, missing=(header,)))
            if findings:
                raise Refusal(*findings)
            text = out.read_text()
            _, files = _lines(text, root, overlay, unit.path)
            files += [name for name in headers(snapshot, unit, _search(snapshot, version))[0] if name not in files]
            deps = [(path, _pin(snapshot, path)) for path in files]
            return json.dumps({"text": text, "deps": deps}).encode()
        value = json.loads(store.cached(config, "view", key, produce))
        table = _lines(value["text"], root, overlay, unit.path)[0] if lines else ()
        return SourceView(unit.path, version, key, value["text"],
                          tuple((path, sha) for path, sha in value["deps"]), table)
def _one(item: tuple[Snapshot, UnitSpec, str, Recipe]) -> SourceView:
    return get(*item)
def all_versions(snapshot: Snapshot, unit: UnitSpec, recipe: Recipe,
                 versions: Sequence[str]) -> dict[str, SourceView]:
    with effort.stage("view.all_versions"):
        results = pool.map(snapshot.config, "view.all_versions", _one,
                           [(snapshot, unit, version, recipe) for version in versions])
        return dict(zip(versions, results, strict=True))
def active_lines(view: SourceView, path: str) -> dict[int, str]:
    with effort.stage("view.active_lines"):
        text = view.text.splitlines()
        return {source: text[output - 1] for output, file, source in view.lines if file == path}

def diagnostic(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, text: str) -> str:
    """Map physical view lines and out-of-file coordinates; valid source coordinates stay source coordinates."""
    def fix(match: re.Match[str]) -> str:
        path, _ = _path(match[1], snapshot.config.project.root.resolve(), None)
        line, raw = int(match[2]), None if path.endswith(".i") else snapshot.peek(path)
        if not path.endswith((".i", ".h")) or (raw is not None and line <= len(raw.splitlines())):
            return f"{path}:{line}"
        source = get(snapshot, unit, version, recipe)
        path, line = next(((p, n) for output, p, n in source.lines if output == line), (path, line))
        return f"{path}:{line}"
    return re.sub(r"([\w./-]+):(\d+)", fix, text)
