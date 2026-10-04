"""Carry exact published evidence across proved layout-generated rewrites."""

from __future__ import annotations

import copy
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Policy, Project
from unbake.project_tools import atomic as atomic_files
from unbake.typemap import storage


def retained(project: Project, wanted: set[str]) -> dict[str, bytes]:
    """Authenticate retained Git blobs with the receipt's SHA-256, never a revision name."""
    result: dict[str, bytes] = {}
    for digest in wanted:
        path = project.build / "types/sources" / (digest + ".c")
        if path.is_file():
            data = path.read_bytes()
            if storage.digest(data) == digest:
                result[digest] = data
    if result.keys() >= wanted:
        return result
    drafts = getattr(project, "drafts", project.build / "drafts")
    for path in drafts.rglob("*.c"):
        data = path.read_bytes()
        digest = storage.digest(data)
        if digest in wanted:
            result[digest] = data
    if result.keys() >= wanted:
        return result
    listing = subprocess.run(
        ["git", "rev-list", "--objects", "--all", "--", str(project.src.relative_to(project.root))],
        cwd=project.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode:
        return result
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=project.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as reader:
        assert reader.stdin is not None and reader.stdout is not None
        for line in listing.stdout.splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2 or not parts[1].endswith(".c"):
                continue
            reader.stdin.write((parts[0] + "\n").encode())
            reader.stdin.flush()
            header = reader.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                continue
            data = reader.stdout.read(int(header[2]))
            reader.stdout.read(1)
            digest = storage.digest(data)
            if digest in wanted:
                result[digest] = data
                if result.keys() >= wanted:
                    break
        reader.stdin.close()
    return result


def _without_imports(text: str, generated: set[str]) -> str:
    from unbake.layout import apply

    return apply._INCLUDE.sub(lambda include: "" if include[1] in generated else include[0], text)


def _declarations(text: str, bodies: list[str], member: str) -> str:
    from unbake.layout import redeclarations

    text, _ = redeclarations.privatize_tags(text, bodies, member)
    text = redeclarations.strip(text, bodies)
    tags = set().union(*(redeclarations._tags(body) for body in bodies))
    # Publication can retain the solver's fixed-width unknown carrier locally
    # when its former header provider is consumed by grouping.
    text = re.sub(
        r"^typedef[ \t]+s(8|16|32|64)[ \t]+M2C_UNK(8|16|32|64)?;[ \t]*\n",
        lambda carrier: "" if carrier[1] == (carrier[2] or "32") else carrier[0],
        text,
        flags=re.M,
    )
    # Regrouping can replace an imported aggregate alias with a source-local
    # forward alias. Only the identity alias of an imported tag is generated.
    result = re.sub(
        r"^typedef[ \t]+(?:struct|union|enum)[ \t]+(\w+)[ \t]+\1;[ \t]*\n",
        lambda alias: "" if alias[1] in tags else alias[0],
        text,
        flags=re.M,
    )
    return re.sub(r"\A(?:[ \t]*\n)+", "", result)


def _legacy_replay(text: str, current: str, generated: set[str], member: str) -> str:
    """Replay historical include spacing and deterministic private tag spellings."""
    from unbake.decomp.header_declarations import declaration_source
    from unbake.layout import apply, redeclarations

    # The former include matcher consumed blank lines before removed imports.
    # Replay only that deletion, preserving all other whitespace and comments.
    pattern = re.compile(r"(?:^[ \t]*\n)*" + apply._INCLUDE.pattern, re.M)
    text = pattern.sub(lambda match: "" if match[1] in generated else match[0], text)
    names = redeclarations.local_tags(text)
    current_tags = redeclarations.local_tags(current)
    edits = []
    for match in re.finditer(r"\b(?:struct|union|enum)\s+(?P<tag>\w+)", declaration_source(text)):
        name = match["tag"]
        target = name + "_" + member
        if name in names and name not in current_tags and target in current_tags:
            edits.append((match.start("tag"), match.end("tag"), target))
    for start, end, target in reversed(edits):
        text = text[:start] + target + text[end:]
    return text


def _comments(text: str) -> list[str]:
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//(?:\\\n|[^\n])*'
    return [match[0] for match in re.finditer(pattern, text, re.S) if match[0].startswith(("/*", "//"))]


def _comments_match(original: str, current: str) -> bool:
    """Normalization may remove a declaration, never edit comments it retains."""
    from unbake.decomp.header_declarations import declaration_source
    from unbake.layout import redeclarations

    remaining = iter(_comments(original))
    for comment in _comments(current):
        if not any(prior == comment for prior in remaining):
            return False

    def regions(text: str) -> list[str]:
        spans = redeclarations.spans(text)
        spans.extend(span for rows in redeclarations.tag_definitions(text).values() for span in rows)
        return [text[start:end] for start, end in spans]

    authenticated: dict[str, list[list[str]]] = {}
    for region in regions(original):
        key = re.sub(r"\s+", "", declaration_source(region))
        authenticated.setdefault(key, []).append(_comments(region))
    for region in regions(current):
        key = re.sub(r"\s+", "", declaration_source(region))
        if key in authenticated and _comments(region) not in authenticated[key]:
            return False
    return True


def prepare(
    project: Project,
    sources: dict[Path, str],
    outputs: dict[Path, bytes | Path],
    ownership: Any,
    lookup: dict[str, Any],
    previous: set[str],
) -> dict[str, Any] | None:
    """Accept only fresh bytes or an exact replay of our transformation of proved bytes."""
    from unbake.layout import apply

    path = project.build / "types/proven.json"
    if not path.is_file():
        return None
    value = copy.deepcopy(storage.read(path, "types.feedback"))
    storage.validate_identity(project, value, "types.feedback")
    stale = {
        row["source_sha256"]
        for row in value["records"].values()
        if not (project.root / row["source"]).is_file()
        or storage.file_digest(project.root / row["source"]) != row["source_sha256"]
    }
    originals = retained(project, stale) if stale else {}
    for row in value["records"].values():
        target = project.root / row["source"]
        if target not in sources:
            raise Held(
                "layout", f"types.feedback.source_sha256: published source missing: {target}; re-prove with submit"
            )
        current = sources[target].encode()
        old_digest = row["source_sha256"]
        if storage.digest(current) != old_digest:
            original = originals.get(old_digest)
            generated = previous | set(lookup["headers"])
            if original is not None:
                old_imports = set(apply._INCLUDE.findall(original.decode()))
                new_imports = set(apply._INCLUDE.findall(current.decode()))
                for home in old_imports:
                    relocated = home.removeprefix("shared/")
                    if home.startswith("shared/") and relocated in new_imports:
                        generated.update((home, relocated))
                    if home.startswith("shared/") and Path(home).stem.lower() in {
                        member.lower() for member in ownership.owners
                    }:
                        generated.add(home)
                    if re.fullmatch(
                        r"shared/types/(?:aliases_.+|consumer_alias_.+|layout_.+|types|typedef_.+)_[a-f0-9]{12,64}\.h",
                        home,
                    ):
                        generated.add(home)

            candidate = None
            canonical = all(
                match[0] == f'#include "{match[1]}"\n'
                for match in apply._INCLUDE.finditer(current.decode())
                if match[1] in generated
            )
            if original is not None and canonical:
                bodies = apply.imported(current.decode(), project.include[0], outputs)
                candidate = _declarations(_without_imports(original.decode(), generated), bodies, target.stem)
                expected = _declarations(_without_imports(current.decode(), generated), bodies, target.stem)
            else:
                expected = _without_imports(current.decode(), generated)
            if original is not None and not _comments_match(
                _legacy_replay(original.decode(), current.decode(), generated, target.stem), current.decode()
            ):
                candidate = None
                canonical = False
            if candidate != expected and original is not None and canonical:
                replay = _legacy_replay(original.decode(), current.decode(), generated, target.stem)
                candidate = _declarations(_without_imports(replay, generated), bodies, target.stem)
            if candidate != expected:
                raise Held(
                    "layout", f"types.feedback.source_sha256: published source changed: {target}; re-prove with submit"
                )
        data = outputs[target]
        assert isinstance(data, bytes)
        row["source_sha256"] = storage.digest(data)
        if row["source_sha256"] != old_digest:
            row["layout_proof"] = {
                "from_source_sha256": old_digest,
                "source_sha256": row["source_sha256"],
                "rom_sha1": storage.identity(project)["rom_sha1"],
            }
        # Retain the original matched proof and its source identity as provenance.
    return {**storage.identity(project), "records": value["records"]}


def keep_sources(project: Project, receipts: dict[str, Any]) -> None:
    """Retain the exact bytes pinned by each newly published layout receipt."""
    for row in receipts["records"].values():
        content = (project.root / row["source"]).read_bytes()
        digest = row["source_sha256"]
        if storage.digest(content) != digest:
            raise Held("layout", f"layout.inputs_stale: source changed during proof: {row['source']}")
        path = project.build / "types/sources" / (digest + ".c")
        if not path.is_file() or storage.file_digest(path) != digest:
            storage.write(path, content)


def publish(
    project: Project,
    policy: Policy | None,
    value: dict[str, Any],
    outputs: dict[Path, bytes | Path],
    sources: dict[Path, str],
    receipts: dict[str, Any] | None,
) -> int:
    """Publish freshness only after the ordinary whole-cartridge build accepts the layout."""
    from unbake.layout import apply, index
    from unbake.project import build

    if "inputs_sha256" not in value:
        return apply.install(project, outputs)
    generated = index.headers(project) | {project.root / name for name in value.get("rendered_sha256", {})}
    allowed = {storage.relative(project, p) for p in generated}
    if receipts is not None:
        allowed.update(row["source"] for row in receipts["records"].values())
    allowed.update(("layout.toml", "build/types/proven.json"))
    for name, digest in value["inputs_sha256"].items():
        if name in allowed or name.startswith("declaration-feedback:"):
            continue
        path = (
            Path(name.removeprefix("declaration-source:"))
            if name.startswith("declaration-source:")
            else project.root / name
        )
        if not path.is_file() or storage.file_digest(path) != digest:
            raise Held("layout", f"types.inputs_stale: input changed: {name}; run unbake map then unbake solve")
    database = project.build / "types/database.json"
    proven = project.build / "types/proven.json"
    summary = project.build / "types/summary.json"
    redraft = project.build / "types/redraft.json"
    touched = set(outputs) | (index.headers(project) - outputs.keys()) | {database, summary, redraft}
    if receipts is not None:
        touched.add(proven)
    backups = {p: p.read_bytes() if p.is_file() else None for p in touched}
    with build.lock(project):
        try:
            count = apply.install(project, outputs)
            if policy is None:
                from unbake.project.config import read_policy

                policy = read_policy()
            results = build.build(
                project,
                policy,
                project.versions,
                tree=project.root,
                generation_for=lambda version: build.current_generation(project, version),
            )
            if any(not result.ok for result in results.values()):
                raise Held("layout", "layout.proof: rewritten project failed cartridge check; freshness unchanged")
            if receipts is not None:
                keep_sources(project, receipts)
                data = storage.encoded(receipts)
                if proven.read_bytes() != data:
                    storage.write(proven, data)
                    count += 1
            value["inputs_sha256"] = storage.inputs(project, headers=True)
            value["rendered_sha256"] = {
                storage.relative(project, p): storage.digest(data)
                for p, data in outputs.items()
                if isinstance(data, bytes) and p not in sources
            }
            data = storage.encoded(value)
            if database.read_bytes() != data:
                storage.write(database, data)
                count += 1
            digest = storage.file_digest(database)
            for path in (summary, redraft):
                if not path.is_file():
                    continue
                metadata = storage.read(path, "types.publication")
                storage.validate_identity(project, metadata, "types.publication")
                if path == summary:
                    metadata["database_sha256"] = digest
                else:
                    for mark in metadata.get("functions", {}).values():
                        mark["type_db_sha256"] = digest
                data = storage.encoded(metadata)
                if path.read_bytes() != data:
                    storage.write(path, data)
                    count += 1
            return count
        except BaseException:
            for path, original in backups.items():
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    storage.write(path, original)
            raise


@contextmanager
def transaction(project: Project) -> Iterator[None]:
    """Keep the source/header/type publication reversible across solve and build."""
    paths = {p for root in (*project.include, project.src) for p in root.rglob("*") if p.is_file()}
    paths.update(p for folder in ("types", "layout", "map") for p in (project.build / folder).glob("*.json"))
    paths.update(project.version(version).split for version in project.versions)
    with tempfile.TemporaryDirectory(prefix=".layout-backup-", dir=project.build) as directory:
        backups = {}
        for number, path in enumerate(sorted(paths)):
            backup = Path(directory) / str(number)
            atomic_files.copy2(path, backup)
            backups[path] = backup
        try:
            yield
        except BaseException:
            after = {p for root in (*project.include, project.src) for p in root.rglob("*") if p.is_file()}
            after.update(p for folder in ("types", "layout", "map") for p in (project.build / folder).glob("*.json"))
            for path in after - paths:
                path.unlink()
            for path, backup in backups.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.copy2(backup, path)
            raise


def recover_index(project: Project, ownership: Any) -> dict[str, Any]:
    """Recover only ownership-map destinations from a missing publication index."""
    from unbake.decomp.header_declarations import declarations
    from unbake.layout import index

    lookup = index.load(project)
    if lookup["headers"]:
        return lookup
    names = {group.header for group in ownership.groups}
    segments = {group.segment for group in ownership.groups}
    if project.versions:
        from unbake.typemap.database import symbol_segments

        segments.update(symbol_segments(project).values())
    names.update(f"{segment}/{kind}.h" for segment in segments for kind in ("types", "data"))
    names.update(("common/types.h", "common/data.h"))
    lookup["type_headers"] = {}
    for name in sorted(names):
        path = project.include[0] / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        lookup["headers"][name] = storage.digest(data)
        row = declarations(data.decode())
        for symbol in row.declared | row.exports | row.typedefs:
            lookup["symbols"].setdefault(symbol, name)
        for type_ in row.typedefs | row.tags:
            lookup["type_headers"].setdefault(type_, []).append(name)
    return lookup


def refresh(project: Project, policy: Policy | None, value: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Replay retained cutover evidence, then solve the installed declaration context once."""
    from unbake.layout import index, map
    from unbake.typemap import solver

    ownership = map.load(project)
    previous = set(index.load(project)["headers"])
    previous.update(
        str((project.root / name).relative_to(project.include[0]))
        for name in value.get("rendered_sha256", {})
        if (project.root / name).is_relative_to(project.include[0])
    )
    # Regrouping also consumes authored declaration components. Authenticate
    # their old include spellings with the solved input inventory.
    previous.update(
        str((project.root / name).relative_to(project.include[0]))
        for name in value["inputs_sha256"]
        if (project.root / name).is_relative_to(project.include[0])
        and name.endswith(".h")
        and not (project.root / name).is_file()
    )
    try:
        current = storage.inputs(project, headers=True)
    except Held as error:
        if not error.reason.startswith("types.feedback.source_sha256:"):
            raise
        current = None
    if current == value["inputs_sha256"]:
        return value, previous
    lookup = recover_index(project, ownership)
    sources = {path: path.read_text() for path in project.src.rglob("*.c")}
    outputs: dict[Path, bytes | Path] = {
        project.include[0] / name: (project.include[0] / name).read_bytes() for name in lookup["headers"]
    }
    outputs.update({path: text.encode() for path, text in sources.items()})
    receipts = prepare(project, sources, outputs, ownership, lookup, previous)
    if policy is None:
        from unbake.project.config import read_policy

        policy = read_policy()
    # These receipts are provisional inside apply's transaction. The final
    # rendered cartridge check in publish is the acceptance proof; rebuilding
    # the unchanged installed context first adds no proof for the proposal.
    for path, data in outputs.items():
        assert isinstance(data, bytes)
        if storage.file_digest(path) != storage.digest(data):
            raise Held("layout", f"layout.inputs_stale: changed during proof: {path}")
    if receipts is not None:
        keep_sources(project, receipts)
        storage.write(project.build / "types/proven.json", storage.encoded(receipts))
    if lookup["headers"] and not index.path(project).is_file():
        storage.write(index.path(project), index.encoded(lookup))
    return solver.solve(project, policy), previous
