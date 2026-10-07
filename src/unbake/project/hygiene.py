"""Keep host inputs and generated outputs outside the tracked project."""

import io
import subprocess
from pathlib import Path

from unbake.config import Held, Host, Project
from unbake.process import capture, temporary_environment
from unbake.process import named as cause_named


def compiler_directories(project: Project) -> tuple[Path, ...]:
    return tuple(project.tools.relative_to(project.root) / ident for ident in sorted(project.compilers))


def base_ignore_text(root: Path) -> str:
    """Repository rules shared by an empty shell and a ready project."""
    entries = [
        "/roms/",
        "baserom.*",
        "*.z64",
        "*.n64",
        "*.v64",
        "/build/",
        "/asm/",
        "/.splat/",
        "/.unbake/",
        "/.attempts.lock",
        "__pycache__/",
        "*.py[cod]",
        "*.lock",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa*",
        "credentials.json",
    ]
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", ":(top)baserom.sha1", ":(top,glob)**/baserom.sha1"],
        cwd=root,
        env=temporary_environment(root / "build"),
        capture_output=True,
        check=False,
    )
    if tracked.returncode == 0 and tracked.stdout:
        entries.insert(2, "!baserom.sha1")
    return "\n".join(entries) + "\n"


def runtime_ignore_text(project: Project) -> str:
    """Preserve authored ignore rules and append only the sole Ledger runtime lock."""
    path = project.root / ".gitignore"
    text = path.read_text() if path.is_file() else ""
    if text.splitlines() and text.splitlines()[-1] == "/.attempts.lock":
        return text
    return text.rstrip("\n") + ("\n" if text else "") + "/.attempts.lock\n"


def ignore_text(project: Project) -> str:
    path = project.root / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    entries = [
        *base_ignore_text(project.root).splitlines(),
        f"/{project.roms.relative_to(project.root).as_posix()}/",
        f"/{project.build.relative_to(project.root).as_posix()}/",
        "/.splat/",
        "/.unbake/",
    ]
    entries.extend(f"/{directory.as_posix()}/" for directory in compiler_directories(project))
    entries.append(f"/{project.tools.relative_to(project.root).as_posix()}/.downloads/")
    required = {entry.removeprefix("/") for entry in entries if entry.startswith("/") or entry.endswith("/")}
    lines: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        # Ignore spelling differences in the root rules supplied by the tool.
        # Retain the first spelling and all comments, blanks and negations.
        root_rule = line.removeprefix("/")
        key = root_rule if root_rule in required else line
        if line and not line.startswith(("#", "!")):
            if key in seen:
                continue
            seen.add(key)
        lines.append(line)
    for entry in entries:
        key = entry.removeprefix("/")
        if key not in seen:
            lines.append(entry)
            seen.add(key)
    # The tracked identity file must remain visible after every ignore rule.
    if "!baserom.sha1" in entries:
        lines = [line for line in lines if line != "!baserom.sha1"]
        lines.append("!baserom.sha1")
    return "\n".join(lines) + "\n"


def indexed_contents(root: Path, blobs: list[bytes]) -> dict[bytes, bytes]:
    if not blobs:
        return {}
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        env=temporary_environment(root / "build"),
        input=b"\n".join(blobs) + b"\n",
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise Held(
            cause_named(
                "project.hygiene.indexed_contents",
                f"git cat-file: {result.stderr.decode(errors='replace').strip()}",
                owner="project.hygiene",
                stage="check",
            )
        )
    stream = io.BytesIO(result.stdout)
    contents = {}
    for blob in blobs:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise Held(
                cause_named(
                    "project.hygiene.indexed_contents",
                    f"indexed object {blob.decode()}: missing blob",
                    owner="project.hygiene",
                    stage="check",
                )
            )
        contents[blob] = stream.read(int(header[2]))
        stream.read(1)
    return contents


def tracked_findings(project: Project, policy: Host) -> list[str]:
    """Inspect indexed names and regular working files without following links."""
    if not (project.root / ".git").exists():
        return []
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=project.root,
        env=temporary_environment(project.build),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise Held(
            cause_named(
                "project.hygiene.tracked_findings",
                f"git ls-files: {result.stderr.decode(errors='replace').strip()}",
                owner="project.hygiene",
                stage="check",
            )
        )
    directories = compiler_directories(project)
    prefixes = tuple("/" + name + "/" for name in ("home", "mnt", "opt"))
    policy_paths = tuple(
        str(value) for value in (policy.cache_machine_root, policy.n64link, policy.cpp) if Path(value).is_absolute()
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
        if path.is_relative_to(project.roms) or path.is_relative_to(project.build) or relative.parts[:1] == ("asm",):
            findings.append(f"HELD(check): {name}: tracked ROM or generated output")
            continue
        if any(relative.is_relative_to(directory) for directory in directories):
            findings.append(f"HELD(check): {name}: tracked compiler file (local-only directory)")
            continue
        try:
            candidates = {contents[blob]}
            if path.is_file():
                candidates.add(path.read_bytes())
        except OSError as error:
            raise Held(
                capture(error, cause=cause_named(f"{name}", f"{name}: {error}", owner="project.hygiene", stage="check"))
            ) from error
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
