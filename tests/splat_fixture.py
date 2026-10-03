"""Minimal create_config response; Unbake still computes its executable layout."""

import subprocess
from pathlib import Path


def create(command, *, cwd, **kwargs):
    assert command[1:] == ["create_config", "input.z64"], command
    image = (Path(cwd) / "input.z64").read_bytes()
    if len(image) == 0x1200:
        template = (
            "name: UNBAKE\nsha1: fixture\noptions:\n  basename: input\n  platform: n64\n  compiler: IDO\n"
            "segments:\n  - [0, header, header]\n  - [0x40, bin, boot]\n"
            "  - name: entry\n    type: code\n    start: 0x1000\n    vram: 0x80000400\n    subsegments:\n"
            "      - [0x1000, hasm]\n"
            "  - name: main\n    type: code\n    start: 0x1040\n    vram: 0x80000440\n"
            "    bss_size: 0x20\n    subsegments:\n      - [0x1040, asm]\n      - [0x1058, data]\n"
            "      - {type: bss, vram: 0x80000600}\n"
            "  - [0x1200]\n"
        )
        (Path(cwd) / "input.yaml").write_text(template)
        return subprocess.CompletedProcess(command, 0, "", "")
    (Path(cwd) / "input.yaml").write_text(
        "name: UNBAKE\nsha1: fixture\noptions:\n  basename: input\n  platform: n64\n  compiler: IDO\n"
        "segments:\n  - [0, header, header]\n  - [0x40, bin, boot]\n"
        "  - name: entry\n    type: code\n    start: 0x1000\n    vram: 0x80000400\n"
        "    subsegments:\n      - [0x1000, asm, main]\n  - [0x" + format(len(image), "X") + "]\n"
    )
    return subprocess.CompletedProcess(command, 0, "", "")
