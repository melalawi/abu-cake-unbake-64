#!/usr/bin/env python3
"""Publish build outputs without ever opening a shared destination for writing."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import IO, Any

from unbake.process import temporary_environment

_recorder: ContextVar[Callable[[Path], None] | None] = ContextVar("atomic.recorder", default=None)


def before_write(path: Path) -> None:
    """Notify the owning operation before changing one destination."""
    callback = _recorder.get()
    if callback is not None:
        callback(Path(path))


@contextmanager
def recording(callback: Callable[[Path], None]) -> Iterator[None]:
    token = _recorder.set(callback)
    try:
        yield
    finally:
        _recorder.reset(token)


def remove(path: Path, *, missing_ok: bool = False) -> None:
    before_write(path)
    path.unlink(missing_ok=missing_ok)


@contextmanager
def staging(path: Path, *, durable: bool = True) -> Iterator[Path]:
    """Yield a nonexistent private path on the destination filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Not the destination's suffix: a reader globbing src/*.c or include/*.h must never see a staging file.
    descriptor, name = tempfile.mkstemp(prefix=".publish-", suffix=".partial", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        yield temporary
        publish(temporary, path, durable=durable)
    finally:
        temporary.unlink(missing_ok=True)


def publish(temporary: Path, path: Path, *, durable: bool = True) -> None:
    """Sync a private file, then replace the destination directory entry."""
    if durable:
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
    before_write(path)
    os.replace(temporary, path)


@contextmanager
def stream(
    path: Path,
    mode: str = "w",
    *,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    permissions: int | None = None,
    durable: bool = True,
) -> Iterator[IO[Any]]:
    """Write privately; append under a stable side lock and publish on success.

    durable=False skips fsync: for re-derivable output (caches, scratch), whose loss only costs a recompute."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if "a" in mode or "+" in mode:
            lock_path = path.with_name("." + path.name + ".append.lock")
            before_write(lock_path)
            lock = stack.enter_context(lock_path.open("a+b"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        if permissions is None:
            permissions = path.stat().st_mode & 0o777 if path.exists() else 0o644
        with staging(path, durable=durable) as temporary:
            if ("a" in mode or "+" in mode) and path.exists():
                shutil.copyfile(path, temporary)
            with temporary.open(mode, encoding=encoding, errors=errors, newline=newline) as output:
                yield output
                output.flush()
                if durable:
                    os.fsync(output.fileno())
            temporary.chmod(permissions)


def write(path: Path, content: bytes | bytearray, *, mode: int | None = None, durable: bool = True) -> None:
    with stream(path, "wb", permissions=mode, durable=durable) as output:
        output.write(content)


def fresh(path: Path, content: bytes) -> None:
    """Fill a private path that a cache producer renames into place; a re-derivable entry is not synced."""
    before_write(path)
    with path.open("xb") as output:
        output.write(content)


def text(
    path: Path,
    content: str,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    *,
    durable: bool = True,
) -> int:
    with stream(path, encoding=encoding, errors=errors, newline=newline, durable=durable) as output:
        return int(output.write(content))


def copyfile(source: Path, destination: Path, *, follow_symlinks: bool = True, durable: bool = True) -> Path:
    with staging(destination, durable=durable) as temporary:
        shutil.copyfile(source, temporary, follow_symlinks=follow_symlinks)
    return destination


def copy2(source: Path, destination: Path, *, follow_symlinks: bool = True, durable: bool = True) -> Path:
    destination = Path(destination)
    if destination.is_dir():
        destination /= Path(source).name
    with staging(destination, durable=durable) as temporary:
        shutil.copy2(source, temporary, follow_symlinks=follow_symlinks)
    return destination


def copy(source: Path, destination: Path, *, follow_symlinks: bool = True) -> Path:
    destination = Path(destination)
    if destination.is_dir():
        destination /= Path(source).name
    with staging(destination) as temporary:
        shutil.copy(source, temporary, follow_symlinks=follow_symlinks)
    return destination


def copytree(source: Path, destination: Path, **kwargs: Any) -> Path:
    kwargs.setdefault("copy_function", copy2)
    if kwargs["copy_function"] not in (copy2, os.link):
        raise ValueError("tree copies require atomic publication or hardlinks")
    return Path(shutil.copytree(source, destination, **kwargs))


def receipt(path: Path) -> None:
    """Advance a receipt's timestamp by replacing its inode, preserving bytes."""
    # Proved outputs are durable already; this replacement advances only a timestamp.
    with staging(path, durable=False) as temporary:
        if path.exists():
            shutil.copyfile(path, temporary)
            temporary.chmod(path.stat().st_mode & 0o777)
        else:
            temporary.touch()


def command(outputs: list[Path], argv: list[str]) -> None:
    """Redirect exact output arguments to fresh files; publish only on success."""
    if not outputs or len(set(outputs)) != len(outputs):
        raise ValueError("expected distinct output paths")
    if any(argv.count(str(path)) != 1 for path in outputs):
        raise ValueError("each output must appear exactly once in the command")
    with ExitStack() as stack:
        names = {str(path): str(stack.enter_context(staging(path))) for path in outputs}
        subprocess.run(
            [names.get(word, word) for word in argv], env=temporary_environment(outputs[0].parent), check=True
        )
        if any(not Path(name).is_file() for name in names.values()):
            raise ValueError("command did not produce every declared output")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--touch", type=Path)
    parser.add_argument("--output", type=Path, action="append", default=[])
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.touch is not None:
            if args.output or args.argv:
                raise ValueError("--touch cannot be combined with a command")
            receipt(args.touch)
        else:
            command(args.output, args.argv[1:] if args.argv[:1] == ["--"] else args.argv)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"HELD(publish): {error}\n")


if __name__ == "__main__":
    main()
