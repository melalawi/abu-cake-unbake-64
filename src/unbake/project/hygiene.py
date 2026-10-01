"""Keep host inputs and generated outputs outside the tracked project."""

import io
import subprocess
from dataclasses import fields
from pathlib import Path

from unbake.project.config import Held, Policy, Project


def compiler_directories(project: Project) -> tuple[Path, ...]:
    return tuple(project.tools.relative_to(project.root) / ident for ident in sorted(project.compilers))


def ignore_text(project: Project) -> str:
    path = project.root / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    entries = ["__pycache__/", "*.py[cod]", "baserom.*", "/build/", "/asm/", "/.splat/"]
    entries.extend(f"/{directory.as_posix()}/" for directory in compiler_directories(project))
    # Append required rules after user rules so negations cannot expose host inputs.
    suffix = "\n".join(entries) + "\n"
    if existing.endswith(suffix):
        return existing
    return existing + ("\n" if existing and not existing.endswith("\n") else "") + suffix


def indexed_contents(root: Path, blobs: list[bytes]) -> dict[bytes, bytes]:
    if not blobs:
        return {}
    result = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=root, input=b"\n".join(blobs) + b"\n", capture_output=True, check=False
    )
    if result.returncode:
        raise Held("check", f"git cat-file: {result.stderr.decode(errors='replace').strip()}")
    stream = io.BytesIO(result.stdout)
    contents = {}
    for blob in blobs:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise Held("check", f"indexed object {blob.decode()}: missing blob")
        contents[blob] = stream.read(int(header[2]))
        stream.read(1)
    return contents


def tracked_findings(project: Project, policy: Policy) -> list[str]:
    """Inspect indexed names and regular working files without following links."""
    if not (project.root / ".git").exists():
        return []
    result = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=project.root, capture_output=True, check=False)
    if result.returncode:
        raise Held("check", f"git ls-files: {result.stderr.decode(errors='replace').strip()}")
    directories = compiler_directories(project)
    prefixes = tuple("/" + name + "/" for name in ("home", "mnt", "opt"))
    policy_paths = tuple(
        str(value)
        for field in fields(policy)
        if isinstance(value := getattr(policy, field.name), Path) and value.is_absolute()
    )
    records = [record for record in result.stdout.split(b"\0") if record]
    blobs = [record.split(b"\t", 1)[0].split()[1] for record in records]
    contents = indexed_contents(project.root, list(dict.fromkeys(blobs)))
    findings = []
    for record in records:
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, blob, _stage = metadata.split()
        name = raw_name.decode(errors="surrogateescape")
        relative = Path(name)
        path = project.root / relative
        if mode == b"120000" or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            findings.append(f"HELD(check): {name}: tracked symlink")
            continue
        if any(relative.is_relative_to(directory) for directory in directories):
            findings.append(f"HELD(check): {name}: tracked compiler file (local-only directory)")
            continue
        try:
            candidates = {contents[blob]}
            if path.is_file():
                candidates.add(path.read_bytes())
        except OSError as error:
            raise Held("check", f"{name}: {error}") from error
        locations = set()
        for content in candidates:
            if b"\0" in content:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if any(prefix in line for prefix in (*prefixes, *policy_paths)):
                    locations.add(number)
        for number in sorted(locations):
            findings.append(f"HELD(check): {name}:{number}: absolute machine path")
    return findings
