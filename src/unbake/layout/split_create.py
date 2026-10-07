"""Create measured Splat layouts and complete executable boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.config import Held, Host
from unbake.layout import split_analysis
from unbake.process import temporary_environment


def without_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        quote = None
        escaped = False
        for index, character in enumerate(line):
            if escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif quote:
                if character == quote:
                    quote = None
            elif character in "\"'":
                quote = character
            elif character == "#":
                line = line[:index]
                break
        if line.strip():
            lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def create(
    rom: Path,
    stem: str,
    version: str,
    *,
    policy: Host | Host | None = None,
    code_ranges: Sequence[tuple[int, int, int]] | None = None,
) -> str:
    """Keep Splat's measured layout, anchoring every output in a VERSION tree."""
    from unbake.project.rom import stem as valid_stem

    valid_stem(stem, "name")
    valid_stem(version, "VERSION")
    if policy is None:
        raise Held("split", "split.create: the host config (unbake.toml) is required")
    executable = shutil.which(str(policy.splat))
    if executable is None:
        raise Held("init", f"policy.splat {policy.splat}: missing executable")
    rom = Path(rom).resolve()
    data = rom.read_bytes()
    if len(data) < 0x1000 or data[:4] != bytes.fromhex("80371240"):
        raise Held("init", f"{rom.name}: required normalized N64 ROM")
    policy.cache_machine_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".create-", dir=policy.cache_machine_root) as temporary:
        work = Path(temporary)
        # Splat embeds the title as an unquoted YAML scalar before loading it.
        # Give its scratch input a safe title and restore the original facts.
        scratch = bytearray(data)
        scratch[0x20:0x34] = b"UNBAKE".ljust(20, b" ")
        atomic_files.write(work / "input.z64", scratch)
        result = subprocess.run(
            [executable, "create_config", "input.z64"],
            cwd=work,
            env=temporary_environment(work),
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise Held(
                "init", f"splat create_config {rom.name}: exit {result.returncode}: {result.stdout}{result.stderr}"
            )
        outputs = list(work.glob("*.yaml"))
        if len(outputs) != 1:
            raise Held("init", f"splat create_config {rom.name}: expected one YAML, found {len(outputs)}")
        text = without_comments(outputs[0].read_text())
    from unbake.project.header import decode

    title = decode(data[:64]).title
    text = re.sub(r"^name:.*$", "name: " + json.dumps(title), text, flags=re.M)
    text = re.sub(r"^sha1:.*$", "sha1: " + hashlib.sha1(data).hexdigest(), text, flags=re.M)
    text = complete_executable(text, data, code_ranges=code_ranges)
    options, segments = text.split("segments:\n", 1)
    replacements = {
        "basename": stem,
        "target_path": f"roms/baserom.{version}.z64",
        "base_path": "../..",
        "elf_path": f"build/{version}/{stem}.{version}.elf",
        "ld_script_path": f"build/{version}/{stem}.ld",
        "asm_path": f"asm/{version}",
        "src_path": "src",
        "build_path": f"build/{version}",
        "asset_path": f"asm/{version}/assets",
        "symbol_addrs_path": f"versions/{version}/symbol_addrs.txt",
        "reloc_addrs_path": "[]",
        "extensions_path": "tools/splat_ext",
        "generated_asm_macros_directory": f"asm/{version}/include",
        "undefined_funcs_auto_path": f"build/{version}/undefined_funcs_auto.txt",
        "undefined_syms_auto_path": f"build/{version}/undefined_syms_auto.txt",
        "ld_legacy_generation": "true",
        "create_asm_dependencies": "true",
        "o_as_suffix": "false",
    }
    lines = []
    skip_list = False
    for line in options.splitlines():
        if skip_list and re.match(r"\s+- ", line):
            continue
        skip_list = False
        match = re.match(r"  (\w+):", line)
        if match and match[1] in replacements:
            skip_list = not line.split(":", 1)[1].strip()
            continue
        lines.append(line)
    lines.extend(f"  {key}: {value}" for key, value in replacements.items())
    named = []
    for line in segments.splitlines():
        match = re.match(r"(\s*)- \[(0x[\da-fA-F]+), (\w+)\]", line)
        if match:
            indent, offset, kind = match.groups()
            # hasm means landed original asm (decomp.original_asm); splat's entry row starts as plain asm.
            name = "entry" if kind == "hasm" else f"{kind}_{int(offset, 16):06X}"
            line = f"{indent}- [{offset}, {'asm' if kind == 'hasm' else kind}, {name}]"
        if re.match(r"\s*- \{\s*type: bss,", line):
            continue
        named.append(line)
    return "\n".join(lines) + "\nsegments:\n" + "\n".join(named) + "\n"


def loaded_rows(data: bytes, begin: int, end: int, bias: int) -> str:
    """Anchor direct callees so cross-segment jumps cannot merge functions."""
    from unbake.layout import boundary, boundary_signatures

    signatures = boundary_signatures.configured() if os.environ.get("UNBAKE_BOUNDARY_SIGNATURES") else ()
    boundaries = boundary.entries(data, begin, end, bias, signatures)
    return "".join(f"      - [0x{offset:X}, asm]\n" for offset in sorted(boundaries))


def loaded_match(match: re.Match[str], *, data: bytes, end: int, bias: int, offsets: tuple[int, ...]) -> str:
    """Seed each existing row only within its own ROM interval."""
    begin = int(match[1], 0)
    stop = min((offset for offset in offsets if offset > begin), default=end)
    return loaded_rows(data, begin, min(stop, end), bias).rstrip()


def complete_executable(text: str, data: bytes, *, code_ranges: Sequence[tuple[int, int, int]] | None = None) -> str:
    copied = list(code_ranges) if code_ranges is not None else split_analysis.copied_text(data)
    if copied and copied[0][0] == 0x1000:
        # Keep Splat's header/boot facts and replace only proved loaded spans.
        prefix, _ = text.split("  - name: entry\n", 1)
        output = []
        cursor = 0x1000
        for begin, end, bias in copied:
            if begin < cursor:
                continue
            if begin > cursor:
                output.append(f"  - [0x{cursor:X}, bin]\n")
            output.extend(
                (
                    f"  - name: main_{begin:X}\n",
                    "    type: code\n",
                    f"    start: 0x{begin:X}\n",
                    f"    vram: 0x{begin + bias:X}\n",
                    "    align: 4\n",
                    "    subalign: 4\n",
                    "    subsegments:\n",
                    loaded_rows(data, begin, end, bias),
                )
            )
            cursor = end
        output.extend((f"  - [0x{cursor:X}, bin]\n", f"  - [0x{len(data):X}]\n"))
        return prefix + "".join(output)
    lines = text.splitlines(keepends=True)
    blocks = [index for index, line in enumerate(lines) if re.match(r"  - ", line)]
    for index, first in reversed(list(enumerate(blocks))):
        last = blocks[index + 1] if index + 1 < len(blocks) else len(lines)
        block = "".join(lines[first:last])
        if "    type: code\n" not in block or "  - name: main\n" not in block:
            continue
        fields = dict(re.findall(r"^    (\w+):\s*(\S+)\s*$", block, re.M))
        start, vram = int(fields["start"], 0), int(fields["vram"], 0)
        bss = re.search(r"type: bss, vram: (0x[\da-fA-F]+)", block)
        row = re.search(r"^      - \[(0x[\da-fA-F]+), data\]\s*$", block, re.M)
        following = re.search(r"(?:start:\s*|- \[)(0x[\da-fA-F]+)", "".join(lines[last:]))
        measured_end = int(row[1], 0) if row else int(following[1], 0) if following else len(data)
        loaded = split_analysis.loaded_bounds(data) if not bss else None
        # Only an independently measured clear-loop boundary can extend a bin.
        if not bss:
            if loaded is None or following is None:
                continue
            limit = loaded[0] - vram + start
            if limit <= measured_end:
                continue
            suffix = "".join(lines[last:])
            suffix = suffix[: following.start(1)] + f"0x{limit:X}" + suffix[following.end(1) :]
            lines[last:] = suffix.splitlines(keepends=True)
        else:
            limit = int(bss[1], 0) - vram + start
        end = split_analysis.executable_end(
            data, start, limit, vram - start, measured_end=limit if loaded else measured_end
        )
        if end > limit:
            raise Held("init", "executable end: crosses measured BSS boundary")
        if row:
            if end == limit:
                block = block[: row.start()] + block[row.end() :]
            else:
                block = block[: row.start(1)] + f"0x{end:X}" + block[row.end(1) :]
        elif end < limit:
            block += f"      - [0x{end:X}, data]\n"
        if os.environ.get("UNBAKE_BOUNDARY_SIGNATURES"):
            block = re.sub(
                r"^      - \[(0x[\da-fA-F]+), asm\]\s*$",
                partial(
                    loaded_match,
                    data=data,
                    end=end,
                    bias=vram - start,
                    offsets=tuple(int(value, 0) for value in re.findall(r"^      - \[(0x[\da-fA-F]+)", block, re.M)),
                ),
                block,
                flags=re.M,
            )
        if loaded:
            block = block.replace(
                "    subsegments:\n",
                f"    bss_size: 0x{loaded[1] - loaded[0]:X}\n    bss_end: 0x{loaded[1]:X}\n    subsegments:\n",
            )
        if bss and "bss_size" in fields:
            block = block.replace(
                "    subsegments:\n",
                f"    bss_end: 0x{int(bss[1], 0) + int(fields['bss_size'], 0):X}\n    subsegments:\n",
            )
        lines[first:last] = block.splitlines(keepends=True)
    return "".join(lines)
