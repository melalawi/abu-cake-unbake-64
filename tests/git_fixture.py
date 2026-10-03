"""An explicit index snapshot at the Git subprocess boundary."""

import hashlib
import subprocess
from pathlib import Path


class Index:
    def __init__(self, root):
        self.root = root
        self.entries = {}
        (root / ".git").mkdir(exist_ok=True)

    def add(self, name, content=None):
        path = self.root / name
        if content is None:
            content = str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
        self.entries[name] = (
            b"120000" if path.is_symlink() else b"100644",
            hashlib.sha1(content).hexdigest().encode(),
            content,
        )

    def run(self, command, **kwargs):
        assert command[0] == "git", command
        if "ls-files" in command:
            if "--stage" in command:
                output = b"".join(
                    mode + b" " + digest + b" 0\t" + name.encode() + b"\0"
                    for name, (mode, digest, _) in self.entries.items()
                )
            else:
                names = self.entries
                if "--" in command:
                    names = [name for name in names if Path(name).name == "baserom.sha1"]
                output = b"".join(name.encode() + b"\0" for name in names)
        elif "cat-file" in command:
            blobs = {digest: content for _, digest, content in self.entries.values()}
            output = b"".join(
                digest + b" blob " + str(len(blobs[digest])).encode() + b"\n" + blobs[digest] + b"\n"
                for digest in kwargs["input"].splitlines()
            )
        elif "clone" in command:
            (Path(command[-1]) / ".git").mkdir(parents=True)
            output = b""
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, output, b"")
