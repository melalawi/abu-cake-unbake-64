"""The extract step: splat per version into the shared cache, for drafting, explaining and symbol tables.

The build never uses this output (unmatched code is copied from the ROM). Splat runs with every
C and original-asm row turned back into assembly, so landing a function never changes the extraction key.
The unpacked directory holds asm/, include/, splat_symbols.csv and symbol-addresses.txt.
"""

from __future__ import annotations

import ast
import csv
import fcntl
import json
import os
import posixpath
import re
import tarfile
import tempfile
from pathlib import Path

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import inputs
from unbake.cache import Cache, key
from unbake.config import Held, Host, Project

# Bump when the archive an extraction stores changes for the same inputs.
EXTRACT_SCHEMA = 3

_FINGERPRINT_PARTS = ("extract.py",)
# A landed row (c, or hasm for original asm) goes back to asm for splat.
_C_ROW = re.compile(r"^(\s*-\s*\[\s*(?:0[xX][\da-fA-F]+|\d+)\s*,\s*)(?:c|hasm)(\s*,\s*)([^,\]\n]+)([^\n]*\]\s*)$", re.M)
_ALIGN = re.compile(
    r"^(\s*-\s*\[\s*(?:0x[\da-fA-F]+|\d+)\s*,\s*(?:asm|c)\s*,\s*[^,\]\n]+)"
    r",\s*\{\s*align:\s*(?:0x[\da-fA-F]+|\d+)\s*\}(\s*\]\s*)$",
    re.M,
)


def scalar(text: str) -> str:
    text = text.split(" #", 1)[0].strip()
    return str(ast.literal_eval(text)) if text[:1] in {"'", '"'} else text


def assembly_rows(text: str, functions: dict[int, str]) -> str:
    """The split as splat reads it: every C and hasm row as assembly, no alignment annotations, and a landed row cut
    at every function FUNCTIONS (address -> name) places inside it.

    A merge-units pass joins landed rows into one; cut at its members' starts, the merged row reads as the rows it
    replaced, so the extraction (keyed on this text) is the one already made and no merge re-extracts anything."""
    text = _ALIGN.sub(lambda match: match[1] + match[2], text)
    lines = text.splitlines(keepends=True)
    cuts: dict[int, list[str]] = {}
    if functions:
        import bisect

        from unbake.layout import split

        addresses = sorted(functions)
        _, _, segments = split.parse_layout(Path("split"), text)
        for segment in segments:
            if "start" not in segment.fields or "vram" not in segment.fields:
                continue
            bias = split.number(segment.fields["vram"], "vram") - split.number(segment.fields["start"], "start")
            for index, row in enumerate(segment.rows):
                if row.kind not in ("c", "hasm"):
                    continue
                stop = segment.rows[index + 1].start if index + 1 < len(segment.rows) else segment.end
                assert stop is not None
                folder = posixpath.dirname(scalar(row.path))
                # The functions strictly inside the row: one bisect per row, never a scan of every function.
                inside = addresses[
                    bisect.bisect_right(addresses, row.start + bias) : bisect.bisect_left(addresses, stop + bias)
                ]
                indent = row.match["indent"]
                cuts[row.line] = [
                    f'{indent}- [0x{address - bias:X}, asm, "{posixpath.join(folder, functions[address])}"]\n'
                    for address in inside
                ]
    out = []
    for index, line in enumerate(lines):
        out.append(_C_ROW.sub(lambda match: match[1] + "asm" + match[2] + match[3] + match[4], line))
        out.extend(cuts.get(index, ()))
    return "".join(out)


def function_symbols(path: Path) -> dict[int, str]:
    """Each address the symbols file marks type:func, by its first name in order (aliases share an address)."""
    from unbake.layout import split

    _, rows = split.symbols(path)
    found: dict[int, str] = {}
    for name, (address, _, match) in sorted(rows.items(), key=lambda item: item[0]):
        if re.search(r"\btype\s*:\s*func\b", match.string):
            found.setdefault(address, name)
    return found


def splat_rows(project: Project, version: str) -> str:
    """The split splat extracts for VERSION (assembly_rows of the configured split and symbols)."""
    from unbake.cache import parsed

    configured = project.version(version)
    # Read once per process while the split and symbols are unchanged: the step key and each archive ask for it.
    return parsed(
        "extract.splat_rows",
        (configured.split, configured.symbols),
        lambda: assembly_rows(configured.split.read_text(), function_symbols(configured.symbols)),
    )


def _compiler_mode(project: Project) -> str:
    from unbake.compilers.registry import specification

    return specification(project.default_compiler).splat


def _version_key(project: Project, host: Host, version: str) -> str:
    configured = project.version(version)
    return key(
        inputs.digest(Path(__file__), algorithm="sha256", reuse=retention.configured()),
        f"extract-v{EXTRACT_SCHEMA}",
        configured.baserom_sha1,
        splat_rows(project, version),
        inputs.digest(configured.symbols, algorithm="sha256", reuse=retention.configured()),
        inputs.digest(host.splat, algorithm="sha256", reuse=retention.configured()),
        _compiler_mode(project),
    )


def input_key(project: Project, host: Host) -> str:
    return key(*(_version_key(project, host, version) for version in project.versions))


def _options(project: Project, version: str, staging: Path, symbols: Path) -> dict[str, object]:
    configured = project.version(version)
    return {
        "base_path": str(staging),
        "target_path": str(configured.baserom.resolve()),
        "asm_path": str(staging / "asm"),
        "src_path": str(staging / "src"),
        "asset_path": str(staging / "assets"),
        "build_path": str(staging / "build"),
        "ld_script_path": str(staging / "layout.ld"),
        "cache_path": str(staging / "cache"),
        "symbol_addrs_path": str(symbols),
        "undefined_funcs_auto_path": str(staging / "undefined_funcs_auto.txt"),
        "undefined_syms_auto_path": str(staging / "undefined_syms_auto.txt"),
        "generated_asm_macros_directory": str(staging / "include"),
        "ld_legacy_generation": True,
        "create_asm_dependencies": False,
        "dump_symbols": True,
        "extensions_path": str(project.tools / "splat_ext"),
        "compiler": _compiler_mode(project),
    }


def _make_archive(project: Project, host: Host, version: str, destination: Path) -> None:
    configured = project.version(version)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        config = staging / "input.yaml"
        overlay = staging / "outputs.yaml"
        rows = splat_rows(project, version)
        atomic_files.text(config, rows)
        options = _options(project, version, staging, configured.symbols.resolve())
        atomic_files.text(
            overlay, "options:\n" + "".join(f"  {name}: {json.dumps(value)}\n" for name, value in options.items())
        )
        from unbake.process import run_native

        run_native(
            [str(host.splat), "split", str(config), str(overlay)],
            project.root,
            "extract",
            context={"version": version, "split": str(configured.split)},
        )
        from unbake.typemap import storage

        # splat names its generated macro files by the staging path it was given: store that path from the project.
        for generated in sorted((staging / "include").glob("*")):
            written = generated.read_text()
            if str(staging) in written:
                atomic_files.text(generated, written.replace(str(staging), storage.relative(project, staging)))
        dump = staging / ".splat" / "splat_symbols.csv"
        if not dump.is_file():
            raise Held("extract", f"extract.splat.{version}: splat wrote no symbol dump")
        atomic_files.copyfile(dump, staging / "splat_symbols.csv")
        committed = instruction_symbols(staging / "asm", discovered_symbols(dump, symbols_from([configured.symbols])))
        units = unit_addresses(rows)
        for name, address in units.items():
            if name in committed and committed[name] != address:
                raise Held("extract", f"extract.symbols.{version}: split and symbols disagree for {name}")
            committed[name] = address
        atomic_files.text(
            staging / "symbol-addresses.txt",
            "".join(
                f"{name} 0x{value:08X}{' unit' if name in units else ''}\n" for name, value in sorted(committed.items())
            ),
        )
        with tarfile.open(destination, "w") as archive:
            for name in ("asm", "include", "assets", "splat_symbols.csv", "symbol-addresses.txt", "layout.ld"):
                if (staging / name).exists():
                    archive.add(staging / name, arcname=name)


def directory(project: Project, host: Host, version: str) -> Path:
    """The unpacked extraction of one version, computed once per content key."""
    content_key = _version_key(project, host, version)
    store = Cache(project.cache)
    archive = store.produce("extract", content_key, lambda path: _make_archive(project, host, version, path))
    unpacked = archive.with_name(archive.name + ".d")
    if unpacked.is_dir():
        return unpacked
    with (archive.parent / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not unpacked.is_dir():
            pending = Path(tempfile.mkdtemp(prefix=".unpack-", dir=archive.parent))
            with tarfile.open(archive) as bundle:
                bundle.extractall(pending, filter="tar")
            os.replace(pending, unpacked)
    return unpacked


def segments(project: Project, host: Host, version: str) -> Path:
    return directory(project, host, version)


def function_asm(project: Project, host: Host, version: str, row_path: str) -> str:
    """Assembly text of one split row (its asm file, written by splat from the row's path)."""
    path = directory(project, host, version) / "asm" / (row_path + ".s")
    if not path.is_file():
        raise Held("extract", f"extract.asm.{version}: no assembly for row {row_path}")
    return path.read_text()


def symbol_table(project: Project, host: Host, version: str) -> dict[str, int]:
    """Every named address of one version: symbol_addrs, splat discoveries, instruction pairs and units."""
    return read_symbol_table(directory(project, host, version) / "symbol-addresses.txt")


def read_symbol_table(path: Path) -> dict[str, int]:
    """Read the extractor-owned name/address/optional-unit table."""
    result = {}
    for line in path.read_text().splitlines():
        name, value, *_ = line.split()
        result[name] = int(value, 16)
    return result


def symbols_from(paths: list[Path]) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in paths:
        for name, value in re.findall(r"([A-Za-z_.$][\w.$]*)\s*=\s*(0[xX][0-9A-Fa-f]+)\s*;", path.read_text()):
            address = int(value, 16)
            if name in found and found[name] != address:
                raise Held("extract", f"extract.symbols: conflicting symbol {name} in {path}")
            found[name] = address
    return found


def discovered_symbols(path: Path, committed: dict[str, int]) -> dict[str, int]:
    """Splat's explicit addresses, including labels compiled C never names."""
    found = dict(committed)
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            name = row["name"]
            if not re.fullmatch(r"[A-Za-z_.$][\w.$]*", name):
                raise Held("extract", f"extract.symbols: invalid discovered symbol {name}")
            address = int(row["vram_start"], 16)
            if name in found and found[name] != address:
                raise Held("extract", f"extract.symbols: conflicting discovered symbol {name}")
            found[name] = address
    return found


def instruction_symbols(directory: Path, committed: dict[str, int]) -> dict[str, int]:
    """Addresses proved by original HI16/LO16 words, for labels absent from splat's symbol dump."""
    found = dict(committed)
    pattern = re.compile(
        r"/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s*\*/[^\n]*?%(hi|lo)\(([A-Za-z_.$][\w.$]*)\)"
    )
    for path in directory.rglob("*.s"):
        pending: dict[str, list[int]] = {}
        for raw, kind, name in pattern.findall(path.read_text()):
            word = int(raw, 16)
            if kind == "hi" and word >> 26 == 15:
                pending.setdefault(name, []).append((word & 65535) << 16)
            elif kind == "lo":
                low = word & 65535
                low = low - 65536 if low & 32768 else low
                for high in pending.pop(name, []):
                    address = (high + low) & 0xFFFFFFFF
                    if name in found and found[name] != address:
                        raise Held("extract", f"extract.symbols: conflicting address for {name} in {path.name}")
                    found[name] = address
    return found


def unit_addresses(text: str) -> dict[str, int]:
    """Explicit code row placements from the split."""
    found: dict[str, int] = {}
    start = vram = None
    in_code = False
    for line in text.splitlines():
        if re.match(r"^  - ", line):
            start = vram = None
            in_code = False
        match = re.match(r"^    (type|start|vram):\s*(\S+)", line)
        if match:
            field, value = match.groups()
            if field == "type":
                in_code = value == "code"
            elif field == "start":
                start = int(value, 0)
            else:
                vram = int(value, 0)
        row = re.match(r"^      - \[\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*(asm|c)\s*,\s*([^\],]+)", line)
        if row:
            if not in_code or start is None or vram is None:
                raise Held("extract", "extract.split: code segment requires type, start, vram before subsegments")
            rom, _, name = row.groups()
            name = Path(scalar(name)).name
            address = vram + int(rom, 0) - start
            if name in found and found[name] != address:
                raise Held("extract", f"extract.split: conflicting split unit symbol {name}")
            found[name] = address
    return found
