"""Create measured Splat layouts and complete executable boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from unbake.project.config import Held, Policy


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


def create(rom: Path, stem: str, version: str, *, policy: Policy | None = None) -> str:
    """Keep Splat's measured layout, anchoring every output in a VERSION tree."""
    from unbake.project.config import load_policy
    from unbake.project.rom import stem as valid_stem

    valid_stem(stem, "name")
    valid_stem(version, "VERSION")
    policy = load_policy() if policy is None else policy
    executable = shutil.which(str(policy.splat))
    if executable is None:
        raise Held("init", f"policy.splat {policy.splat}: missing executable")
    rom = Path(rom).resolve()
    data = rom.read_bytes()
    if len(data) < 0x1000 or data[:4] != bytes.fromhex("80371240"):
        raise Held("init", f"{rom.name}: required normalized N64 ROM")
    with tempfile.TemporaryDirectory(prefix=".create-", dir=rom.parent) as temporary:
        work = Path(temporary)
        # Splat embeds the title as an unquoted YAML scalar before loading it.
        # Give its scratch input a safe title and restore the original facts.
        scratch = bytearray(data)
        scratch[0x20:0x34] = b"UNBAKE".ljust(20, b" ")
        (work / "input.z64").write_bytes(scratch)
        result = subprocess.run([executable, "create_config", "input.z64"], cwd=work, capture_output=True, text=True)
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
    text = complete_executable(text, data)
    options, segments = text.split("segments:\n", 1)
    replacements = {
        "basename": stem,
        "target_path": f"baserom.{version}.z64",
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
            name = "entry" if kind == "hasm" else f"{kind}_{int(offset, 16):06X}"
            line = f"{indent}- [{offset}, {kind}, {name}]"
        if re.match(r"\s*- \{\s*type: bss,", line):
            continue
        named.append(line)
    return "\n".join(lines) + "\nsegments:\n" + "\n".join(named) + "\n"


OPCODES = frozenset((*range(2, 28), *range(32, 64)))


def instruction(word: int) -> bool:
    op = word >> 26
    if op == 0:
        return word & 63 in {
            0,
            2,
            3,
            4,
            6,
            7,
            8,
            9,
            12,
            13,
            15,
            16,
            17,
            18,
            19,
            20,
            22,
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
            37,
            38,
            39,
            42,
            43,
            44,
            45,
            46,
            47,
            48,
            49,
            50,
            51,
            52,
            54,
            56,
            58,
            59,
            60,
            62,
            63,
        }
    if op == 1:
        return word >> 16 & 31 in {0, 1, 2, 3, 8, 9, 10, 11, 12, 14, 16, 17, 18, 19}
    return op in OPCODES


def executable_end(
    data: bytes, start: int, limit: int, bias: int, *, measured_end: int | None = None, seeds: Sequence[int] = ()
) -> int:
    """Follow control flow, seeding stack frames only inside measured text."""
    limit = min(limit, len(data)) // 4 * 4
    pending = [start, *seeds]
    for offset in range(start, min(limit, measured_end if measured_end is not None else start), 4):
        word = struct.unpack_from(">I", data, offset)[0]
        if word & 0xFFFF0000 in (0x27BD0000, 0x67BD0000) and word & 0x8000:
            saved = any(
                struct.unpack_from(">I", data, at)[0] & 0xFFE00000 == 0xAFA00000
                for at in range(offset + 4, min(offset + 64, limit), 4)
            )
            if saved:
                pending.append(offset)
    seen: set[int] = set()
    end = start
    while pending:
        offset = pending.pop()
        while start <= offset < limit and offset not in seen:
            word = struct.unpack_from(">I", data, offset)[0]
            if not instruction(word):
                break
            seen.add(offset)
            end = max(end, offset + 4)
            op = word >> 26
            branch = op in (1, 4, 5, 6, 7, 20, 21, 22, 23) or (op == 17 and word >> 21 & 31 == 8)
            jump = op in (2, 3)
            indirect = op == 0 and word & 63 in (8, 9)
            if branch or jump or indirect:
                if offset + 8 > limit or not instruction(struct.unpack_from(">I", data, offset + 4)[0]):
                    break
                end = max(end, offset + 8)
                if branch:
                    displacement = word & 0xFFFF
                    displacement -= 0x10000 if displacement & 0x8000 else 0
                    pending.append(offset + 4 + displacement * 4)
                elif jump:
                    target = ((offset + bias + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)
                    pending.append(target - bias)
                if op == 2 or (indirect and word & 63 == 8):
                    break
                offset += 8
            else:
                if op == 0 and word & 63 in (12, 13):
                    break
                offset += 4
    return (end + 15) // 16 * 16


def loaded_bounds(data: bytes) -> tuple[int, int] | None:
    """Measure a loaded program from an entrypoint's bounded zero-store loop."""
    if len(data) < 0x1100:
        return None
    values: dict[int, int] = {0: 0}
    initial: dict[int, int] = {}
    increments: dict[int, int] = {}
    comparisons: dict[int, tuple[int, int]] = {}
    for offset in range(0x1000, 0x1100, 4):
        word = struct.unpack_from(">I", data, offset)[0]
        op, source, target = word >> 26, word >> 21 & 31, word >> 16 & 31
        immediate = word & 0xFFFF
        signed = immediate - 0x10000 if immediate & 0x8000 else immediate
        if op == 15:
            values[target] = immediate << 16
        elif op in (9, 13) and source in values:
            value = (values[source] + signed) & 0xFFFFFFFF if op == 9 else values[source] | immediate
            if source == target and signed == 4 and op == 9:
                increments[target] = offset
            else:
                initial[target] = value
            values[target] = value
        elif op == 0 and word & 63 == 43:
            comparisons[word >> 11 & 31] = (source, target)
        elif op == 5 and target == 0 and signed < 0 and source in comparisons:
            lower, upper = comparisons[source]
            if lower not in increments or lower not in initial or upper not in initial:
                continue
            if offset + 4 + signed * 4 != increments[lower]:
                continue
            delay = struct.unpack_from(">I", data, offset + 4)[0]
            if delay >> 26 != 43 or delay >> 16 & 31 or delay >> 21 & 31 != lower:
                continue
            if delay & 0xFFFF != 0xFFFC:
                continue
            begin, end = initial[lower], initial[upper]
            entry = struct.unpack_from(">I", data, 8)[0]
            rom_end = begin - entry + 0x1000
            if entry < begin < end <= 0x80800000 and 0x1000 < rom_end <= len(data) and rom_end % 4 == 0:
                return begin, end
        elif op in (2, 3) or (op == 0 and word & 63 in (8, 9)):
            break
    return None


def copied_spans(data: bytes) -> list[tuple[int, int]]:
    """Measure ROM copies from constant call arguments and counted word loops."""
    words = struct.unpack(f">{len(data) // 4}I", data[: len(data) // 4 * 4])
    values: dict[int, int] = {0: 0}
    spans: set[tuple[int, int]] = set()
    loads: dict[int, int] = {}
    stores: set[int] = set()
    clobber = -1
    rom_sources: set[int] = set()
    for index in range(0x1000 // 4, len(words)):
        if index == clobber:
            values = {register: value for register, value in values.items() if register == 0 or 16 <= register <= 23}
        word = words[index]
        op, source, target = word >> 26, word >> 21 & 31, word >> 16 & 31
        immediate = word & 65535
        signed = immediate - 65536 if immediate & 32768 else immediate
        if word & 0xFFFF0000 == 0x27BD0000 and signed < 0:
            values, loads, stores = {0: 0}, {}, set()
        if op == 15:
            values[target] = immediate << 16
        elif op in (8, 9, 13) and source in values:
            if op == 13 or source != target or signed not in (4, -1):
                values[target] = (values[source] + signed) & 0xFFFFFFFF if op != 13 else values[source] | immediate
        elif op == 0 and word & 63 in (33, 37) and source in values and target in values:
            values[word >> 11 & 31] = (
                values[source] + values[target] if word & 63 == 33 else values[source] | values[target]
            )
        elif op == 35 and immediate == 0 and source in values:
            loads[target] = values[source]
        elif op == 43 and immediate == 0 and target in loads and source in values:
            address = loads[target]
            if 0xB0000000 <= address < 0xB0000000 + len(data) and 0x80000000 <= values[source] < 0x80800000:
                stores.add(address - 0xB0000000)
        elif op == 5 and target == 0 and signed < 0 and source in values and index + 1 < len(words):
            delay = words[index + 1]
            if delay >> 26 == 9 and delay & 65535 == 65535:
                count = values.get(delay >> 21 & 31, -1) + 1
                for begin in stores:
                    if 0 < count <= len(data) // 4 and begin + count * 4 <= len(data):
                        spans.add((begin, begin + count * 4))
        elif op == 3 and index + 1 < len(words):
            clobber = index + 2
            # Argument setup can occupy the call's delay slot.
            delay = words[index + 1]
            if delay >> 26 == 9 and delay >> 21 & 31 == 0:
                values[delay >> 16 & 31] = delay & 65535
            begin, size = values.get(5, 0), values.get(6, 0)
            if 0xB0000000 <= begin < 0xB0000000 + len(data):
                begin -= 0xB0000000
            if begin in rom_sources and 0x1000 <= begin < len(data) and 0x1000 <= size <= len(data) - begin:
                spans.add((begin, begin + size))
        elif word == 0x03E00008 or not instruction(word):
            values, loads, stores = {0: 0}, {}, set()
        rom_sources.update(
            value - 0xB0000000 for value in values.values() if 0xB0001000 <= value < 0xB0000000 + len(data)
        )
    return sorted(spans)


def copied_text(data: bytes) -> list[tuple[int, int, int]]:
    """Fit loaded mappings against independently measured stack-frame boundaries."""
    result = []
    for begin, limit in copied_spans(data):
        if begin % 4 or limit % 4:
            continue
        words = struct.unpack(f">{(limit - begin) // 4}I", data[begin : limit // 4 * 4])
        frames = {
            begin + index * 4
            for index, word in enumerate(words)
            if word & 0xFFFF0000 in (0x27BD0000, 0x67BD0000) and word & 0x8000
        }
        targets = [
            0x80000000 | ((word & 0x3FFFFFF) << 2)
            for word in words
            if word >> 26 in (2, 3) and word & 0x3FFFFFF < 0x200000
        ]
        if len(frames) < 16 or len(targets) < 32:
            continue
        votes = Counter(
            target - frame
            for target in targets[:: max(1, len(targets) // 128)]
            for frame in frames
            if (target - frame) % 0x400 == 0
        )
        candidates = [(sum(target - bias in frames for target in targets), bias) for bias, _ in votes.most_common(8)]
        candidates.sort(reverse=True)
        if len(candidates) < 2 or candidates[0][0] < 32 or candidates[0][0] < candidates[1][0] * 2:
            continue
        bias = candidates[0][1]
        if not 0x80000000 <= begin + bias < limit + bias <= 0x80800000:
            continue
        seeds = sorted({target - bias for target in targets if target - bias in frames})
        # Calls and stack frames independently agree on this loaded mapping.
        end = executable_end(data, begin, limit, bias, seeds=seeds)
        result.append((begin, end, bias))
    merged: list[tuple[int, int, int]] = []
    for begin, end, bias in result:
        if merged and begin <= merged[-1][1] and bias == merged[-1][2]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), bias)
        else:
            merged.append((begin, end, bias))
    return merged


def loaded_rows(data: bytes, begin: int, end: int, bias: int) -> str:
    """Anchor direct callees so cross-segment jumps cannot merge functions."""
    boundaries = {begin}
    for offset in range(begin, end, 4):
        word = struct.unpack_from(">I", data, offset)[0]
        if word >> 26 == 3:
            target = (((offset + bias + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)) - bias
            if begin <= target < end:
                boundaries.add(target)
    return "".join(f"      - [0x{offset:X}, asm]\n" for offset in sorted(boundaries))


def complete_executable(text: str, data: bytes) -> str:
    copied = copied_text(data)
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
        loaded = loaded_bounds(data) if not bss else None
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
        end = executable_end(data, start, limit, vram - start, measured_end=limit if loaded else measured_end)
        if end > limit:
            raise Held("init", "executable end: crosses measured BSS boundary")
        if row:
            if end == limit:
                block = block[: row.start()] + block[row.end() :]
            else:
                block = block[: row.start(1)] + f"0x{end:X}" + block[row.end(1) :]
        elif end < limit:
            block += f"      - [0x{end:X}, data]\n"
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
