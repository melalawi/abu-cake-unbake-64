"""Verified compiler archives and atomic file publication."""

from __future__ import annotations

import hashlib
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import inputs
from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named

if TYPE_CHECKING:
    from unbake.compilers.registry import Download


def relative(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or any(c.isspace() or c in "\\#" for c in name)
    ):
        raise Held(
            cause_named(
                "compilers.files.relative", f"unsafe file name {name!r}", owner="compilers.files", stage="setup"
            )
        )
    return name


def atomic_bytes(path: Path, content: bytes, *, mode: int | None = None) -> None:
    if not path.is_symlink() and path.exists() and path.read_bytes() == content:
        return
    atomic_files.write(path, content, mode=mode)


def atomic_copy(path: Path, source: Path, *, mode: int) -> None:
    """Publish a staged file without retaining its contents in memory."""
    with atomic_files.staging(path) as temporary:
        atomic_files.copyfile(source, temporary)
        temporary.chmod(mode)


def download(entry: Download, cache: Path) -> Path:
    path = cache / "downloads" / entry.sha256
    if path.exists():
        actual = inputs.digest(path, algorithm="sha256", reuse=retention.configured())
        if actual != entry.sha256:
            raise Held(
                cause_named(
                    f"{path}",
                    f"{path}: sha256 expected {entry.sha256}, found {actual}",
                    owner="compilers.files",
                    stage="setup",
                )
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".download-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(entry.url, timeout=60) as response:
                shutil.copyfileobj(response, stream)
            stream.close()
            actual = inputs.digest(temporary, algorithm="sha256", reuse=retention.configured())
            if actual != entry.sha256:
                raise Held(
                    cause_named(
                        f"{entry.url}",
                        f"{entry.url}: archive sha256 expected {entry.sha256}, found {actual}",
                        owner="compilers.files",
                        stage="setup",
                    )
                )
            atomic_files.publish(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def archive_files(path: Path, wanted: set[str]) -> dict[str, bytes]:
    """Read pinned contents without ever extracting archive paths to disk."""
    found = {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                relative(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise Held(
                        cause_named(
                            f"{path}", f"{path}: archive link {member.filename}", owner="compilers.files", stage="setup"
                        )
                    )
                if not member.is_dir():
                    content = archive.read(member)
                    digest = hashlib.sha256(content).hexdigest()
                    if digest in wanted:
                        found[digest] = content
    else:
        with tarfile.open(path) as archive:
            for tar_member in archive:
                relative(tar_member.name)
                if not tar_member.isdir() and not tar_member.isfile():
                    raise Held(
                        cause_named(
                            f"{path}",
                            f"{path}: unsupported archive entry {tar_member.name}",
                            owner="compilers.files",
                            stage="setup",
                        )
                    )
                if tar_member.isfile():
                    extracted = archive.extractfile(tar_member)
                    if extracted is None:
                        raise Held(
                            cause_named(
                                f"{path}",
                                f"{path}: missing archive content {tar_member.name}",
                                owner="compilers.files",
                                stage="setup",
                            )
                        )
                    with extracted as stream:
                        content = stream.read()
                    digest = hashlib.sha256(content).hexdigest()
                    if digest in wanted:
                        found[digest] = content
    return found


def directory_paths(source: Path, wanted: set[str], algorithm: str) -> dict[str, Path]:
    """Find supplied inputs by content digest, independently of their filenames."""
    if not source.is_dir():
        raise Held(
            cause_named(
                "compilers.files.directory_paths",
                f"supply {source}: missing directory",
                owner="compilers.files",
                stage="setup",
            )
        )
    found = {}
    for candidate in sorted(source.rglob("*")):
        if candidate.is_file():
            try:
                with candidate.open("rb") as stream:
                    digest = hashlib.file_digest(stream, algorithm).hexdigest()
                if digest in wanted:
                    found[digest] = candidate
                    if found.keys() >= wanted:
                        break
            except OSError as error:
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "compilers.files.directory_paths",
                            f"supply {candidate}: {error}",
                            owner="compilers.files",
                            stage="setup",
                        ),
                    )
                ) from error
    return found


def directory_files(source: Path, wanted: set[str], algorithm: str) -> dict[str, bytes]:
    """Read supplied compiler files after finding their digest-pinned paths."""
    return {digest: path.read_bytes() for digest, path in directory_paths(source, wanted, algorithm).items()}


def supplied_files(source: Path, wanted: set[str]) -> dict[str, bytes]:
    if source.is_dir():
        return directory_files(source, wanted, "sha256")
    if source.is_file():
        return archive_files(source, wanted)
    raise Held(
        cause_named(
            "compilers.files.supplied_files",
            f"compiler supply {source}: missing archive/directory",
            owner="compilers.files",
            stage="setup",
        )
    )
