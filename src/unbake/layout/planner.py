"""Joint ROM function and constant planning, before compiler publication."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from itertools import combinations, pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake.layout import boundary, boundary_signatures, rodata_owners, split, split_analysis, split_create
from unbake.layout.rodata_references import collect, words
from unbake.project.census import Census
from unbake.project.config import Held, PendingProject, SetupPolicy
from unbake.project.flow import CrossVersionItem, FunctionRecord, LayoutManifest, ProviderRecord, Span, VersionLayout
from unbake.project.rom import Rom


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def body_identity(image: bytes, start: int, end: int) -> dict[str, str]:
    """Raw body bytes and relocation-normalized body are independent of names."""
    from unbake.layout.xver import _masks

    code = words(image[start:end])
    while len(code) > 2 and code[-1] == 0 and code[-2] != 0x03E00008:
        code.pop()
    return {
        "body_sha256": hashlib.sha256(image[start:end]).hexdigest(),
        "normalized_body_sha256": digest([(word & ~mask, mask) for word, mask in zip(code, _masks(code), strict=True)]),
    }


def symbol_items(versions: dict[str, VersionLayout]) -> dict[str, CrossVersionItem]:
    """Group placements by proved symbol, retaining every independent body."""
    items: dict[str, CrossVersionItem] = {}
    for version, layout in versions.items():
        for f in layout["functions"]:
            item = items.setdefault(
                f["name"],
                CrossVersionItem(
                    name=f["name"], versions=[], placements={}, body_groups={}, evidence={"name_source": version}
                ),
            )
            item["versions"].append(version)
            item["placements"][version] = f
            body = f.get("normalized_body_sha256")
            if body is not None:
                item["body_groups"].setdefault(body, []).append(version)
    return items


def measure(image: bytes, yaml: str, executable: Path, work: Path, version: str) -> split.ExtractedText:
    """Use the pinned disassembler in isolation; no project inputs are published."""
    (work / "input.z64").write_bytes(image)
    (work / "symbols.txt").write_text("")
    options = {
        "base_path": str(work),
        "target_path": str(work / "input.z64"),
        "asm_path": str(work / "asm" / version),
        "src_path": str(work / "src"),
        "asset_path": str(work / "assets"),
        "build_path": str(work / "build"),
        "ld_script_path": str(work / "layout.ld"),
        "cache_path": str(work / "cache"),
        "symbol_addrs_path": str(work / "symbols.txt"),
        "undefined_funcs_auto_path": str(work / "undefined_funcs.txt"),
        "undefined_syms_auto_path": str(work / "undefined_syms.txt"),
        "generated_asm_macros_directory": str(work / "include"),
        "extensions_path": str(work / "extensions"),
        "create_asm_dependencies": False,
    }
    (work / "input.yaml").write_text(yaml)
    (work / "outputs.yaml").write_text(
        "options:\n" + "".join(f"  {key}: {json.dumps(value)}\n" for key, value in options.items())
    )
    result = subprocess.run(
        [str(executable), "split", str(work / "input.yaml"), str(work / "outputs.yaml")],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise Held(
            "setup",
            f"layout.function_boundary: splat exit {result.returncode}: {result.stdout[-3000:]}{result.stderr[-3000:]}",
        )
    return split.extracted_text(SimpleNamespace(asm=work / "asm"), version)


def mappings(image: bytes, ranges: tuple[split.Function, ...]) -> tuple[list[dict[str, int]], list[Span]]:
    """Retain copy destinations independently of later executable placements."""
    copies = split_analysis.copy_evidence(image)
    result = []
    for start, end, address in copies:
        if address is None:
            fitted = {row.address - row.start for row in ranges if start <= row.start < row.end <= end}
            if len(fitted) != 1:
                raise Held("setup", f"layout.loaded_mapping: copy ROM 0x{start:X}: destination missing")
            address = start + fitted.pop()
        result.append(dict(start=start, end=end, address=address, table_entry_bias=0))
    if not result:
        bounds = split_analysis.loaded_bounds(image)
        if bounds is None:
            raise Held("setup", "layout.loaded_mapping: entry clear-loop or copied extent required")
        entry = int.from_bytes(image[8:12], "big")
        result.append(dict(start=0x1000, end=bounds[0] - entry + 0x1000, address=entry, table_entry_bias=0))
    ordered = sorted(result, key=lambda row: row["start"])
    # A boot copy may contain a later overlay's ROM source. The source bytes
    # still have one load provider; retain the original copy extents as evidence.
    cuts = sorted({point for row in ordered for point in (row["start"], row["end"])})
    coverage = []
    for start, end in pairwise(cuts):
        covering = [row for row in ordered if row["start"] <= start < end <= row["end"]]
        if covering:
            row = max(covering, key=lambda item: item["start"])
            coverage.append(Span(start=start, end=end, address=row["address"] + start - row["start"]))
    return ordered, coverage


def constant_spans(
    image: bytes, loaded: list[dict[str, int]], ranges: tuple[split.Function, ...], measured: split.ExtractedText
) -> list[rodata_owners.Span]:
    spans = []
    for row in loaded:
        cuts = sorted(
            {
                row["start"],
                row["end"],
                *(point for f in ranges for point in (f.start, f.end) if row["start"] < point < row["end"]),
            }
        )
        for start, end in pairwise(cuts):
            if any(f.start <= start < end <= f.end for f in ranges):
                continue
            address = row["address"] + start - row["start"]
            spans.append(rodata_owners.Span(address, start, end, 0, "copied"))
    for start, end in measured.data:
        containing = next((f for f in ranges if f.start <= start < end <= f.end), None)
        if containing is not None:
            spans.append(rodata_owners.Span(containing.address + start - containing.start, start, end, 0, "in-text"))
    merged: list[rodata_owners.Span] = []
    for span in sorted(spans, key=lambda item: item.start):
        if (
            merged
            and merged[-1].end == span.start
            and merged[-1].stop == span.address
            and merged[-1].scope == span.scope
        ):
            previous = merged[-1]
            merged[-1] = rodata_owners.Span(previous.address, previous.start, span.end, 0, previous.scope)
        else:
            merged.append(span)
    spans = merged
    # Biased table entries must independently point inside measured executable bytes.
    result = []
    for span in spans:
        votes: Counter[int] = Counter()
        for value in words(image[span.start : span.end]):
            for bias in (0, 0x80000000):
                address = (value + bias) & 0xFFFFFFFF
                if value % 4 == 0 and any(f.address <= address < f.address + f.end - f.start for f in ranges):
                    votes[bias] += 1
        bias = 0x80000000 if votes[0x80000000] >= 2 and not votes[0] else 0
        result.append(rodata_owners.Span(span.address, span.start, span.end, bias, span.scope))
    return sorted(result, key=lambda span: span.address)


def functions(
    image: bytes, version: str, ranges: tuple[split.Function, ...], measured: split.ExtractedText
) -> list[split.Function]:
    result = []
    for f in measured.functions:
        # Directives are retained as explicit data providers, including islands.
        runs = sorted((start, end) for start, end in measured.data if f.start <= start < end <= f.end)
        cuts = sorted({f.start, f.end, *(point for run in runs for point in run)})
        for start, end in pairwise(cuts):
            if any(a <= start < end <= b for a, b in runs):
                continue
            address = f.address + start - f.start
            name = f.name if start == f.start else f"func_{address:08X}"
            result.append(split.Function(version, name, start, end, address, name, "asm", ()))
    return result


def correspondence(
    images: Mapping[str, bytes | Rom],
    inventories: dict[str, list[split.Function]],
    reference: str,
    *,
    evidence: dict[str, dict[int, str]] | None = None,
    symbol_evidence: dict[str, dict[int, dict[str, Any]]] | None = None,
    preserve_names: bool = False,
    loaded_spans: Mapping[str, list[Span]] | None = None,
) -> dict[str, dict[int, str]]:
    """Establish body matches, then independent anchored symbol identity.

    Inventory order names items; ROM order establishes positional identity.
    Different bodies require unambiguous positions and mapped graph agreement.
    Conflicting pairwise proposals are rejected together, rather than letting
    version iteration order choose which repeated occurrence wins.
    """
    from unbake.layout.xver import _masks

    indexes: dict[str, dict[str, list[split.Function]]] = {}
    signatures: dict[tuple[str, int], str] = {}
    functions: dict[tuple[str, int], split.Function] = {}
    for version, ff in inventories.items():
        cartridge_image = images[version]
        image = cartridge_image if isinstance(cartridge_image, bytes) else cartridge_image.image()
        index: dict[str, list[split.Function]] = defaultdict(list)
        for f in ff:
            code = words(image[f.start : f.end])
            while len(code) > 2 and code[-1] == 0 and code[-2] != 0x03E00008:
                code.pop()
            masks = _masks(code)
            signature = digest([(word & ~mask, mask) for word, mask in zip(code, masks, strict=True)])
            index[signature].append(f)
            signatures[version, f.start] = signature
            functions[version, f.start] = f
        indexes[version] = index
        del image
    parent = {key: key for key in functions}

    def root(key: tuple[str, int]) -> tuple[str, int]:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def join(a: tuple[str, int], b: tuple[str, int]) -> None:
        parent[root(b)] = root(a)

    canonical: dict[str, tuple[str, int]] = {}
    for version, index in indexes.items():
        for signature, ff in index.items():
            if len(ff) == 1:
                key = (version, ff[0].start)
                join(canonical.setdefault(signature, key), key)
    reasons = dict.fromkeys(functions, "repeated-body-unbounded")
    proposals: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    ordered = {v: sorted(ff, key=lambda f: f.start) for v, ff in inventories.items()}
    for a, b in combinations(inventories, 2):
        common = {sig for sig, ff in indexes[a].items() if len(ff) == 1 and len(indexes[b].get(sig, [])) == 1}
        anchors = {
            v: [(i, signatures[v, f.start]) for i, f in enumerate(ordered[v]) if signatures[v, f.start] in common]
            for v in (a, b)
        }
        target_pairs = {(left[1], right[1]): (left[0], right[0]) for left, right in pairwise(anchors[b])}
        for (lo, left), (hi, right) in pairwise(anchors[a]):
            source = [(a, f.start) for f in ordered[a][lo + 1 : hi]]
            if (left, right) not in target_pairs:
                for key in source:
                    reasons[key] = "repeated-body-anchor-order"
                continue
            start, end = target_pairs[left, right]
            target = [(b, f.start) for f in ordered[b][start + 1 : end]]
            if [signatures[key] for key in source] != [signatures[key] for key in target]:
                for key in source + target:
                    reasons[key] = "repeated-body-sequence-mismatch"
                continue
            for first, second in zip(source, target, strict=True):
                x, y = root(first), root(second)
                if x != y:
                    proposals[x].add(y)
                    proposals[y].add(x)
    members: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    for key in functions:
        members[root(key)].append(key)
    seen = set()
    positional = set()
    for seed in proposals:
        if seed in seen:
            continue
        component, pending = set(), [seed]
        while pending:
            key = pending.pop()
            if key in component:
                continue
            component.add(key)
            pending.extend(proposals[key] - component)
        seen.update(component)
        rows = [key for group in sorted(component) for key in members[group]]
        if len({v for v, _ in rows}) != len(rows):
            for key in rows:
                reasons[key] = "repeated-body-alignment-conflict"
            continue
        # Every edge was checked against the complete masked sequence.
        assert len({signatures[key] for key in rows}) == 1
        for key in rows:
            join(seed, key)
            positional.add(key)
    from unbake.layout.symbol_identity import join_symbols

    details: dict[tuple[str, int], dict[str, Any]] = {}
    symbolic = (
        join_symbols(images, inventories, root, join, reasons, details, loaded_spans) if len(inventories) > 1 else set()
    )
    groups: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    for key in functions:
        groups[root(key)].append(key)
    names: dict[str, dict[int, str]] = {version: {} for version in inventories}
    used: set[str] = set()
    # Preserve existing naming precedence, including single-ROM repeated bodies.
    precedence = list(dict.fromkeys(root(key) for key in canonical.values()))
    reserved = set(precedence)
    precedence.extend(group for group in groups if group not in reserved)
    for group in precedence:
        rows = groups[group]
        version, start = rows[0]
        f = functions[version, start]
        repeated = len(indexes[version][signatures[version, start]]) > 1
        name = f.name
        if (
            not preserve_names and ((len(rows) == 1 and repeated) or re.fullmatch(r"func_[0-9A-Fa-f]+", name))
        ) or name in used:
            name += "_" + version.replace("-", "_")
        if name in used:
            name += f"_{start:X}"
        used.add(name)
        for v, at in rows:
            names[v][at] = name
            reason = (
                "anchor-call-graph"
                if (v, at) in symbolic
                else "anchor-sequence"
                if (v, at) in positional
                else "unique-body"
                if len(rows) > 1
                else reasons[v, at]
                if len(indexes[v][signatures[v, at]]) > 1 or reasons[v, at].startswith("symbol-")
                else "body-not-shared"
            )
            if evidence is not None:
                evidence.setdefault(v, {})[at] = reason
            if symbol_evidence is not None:
                detail = details.get((v, at), {})
                if (v, at) in symbolic or not reasons[v, at].startswith("symbol-"):
                    detail["reason"] = reason
                else:
                    detail.setdefault("reason", reasons[v, at])
                symbol_evidence.setdefault(v, {})[at] = detail
    return names


def carve(image: bytes, ff: list[split.Function], spans: list[rodata_owners.Span]) -> list[ProviderRecord]:
    refs = []
    for f in ff:
        found, _ = collect(f.name, image[f.start : f.end], None, None)
        refs.extend(found)
    refs = [r for r in refs if any(s.address <= r.address < s.stop for s in spans)]
    objects = rodata_owners.classify(image, ff, spans, refs)
    providers: list[ProviderRecord] = []
    for item in objects:
        span = next(s for s in spans if s.address <= item.address < item.end <= s.stop)
        start = span.start + item.address - span.address
        kind: Any = (
            "private"
            if item.safe_sole_candidate
            else "writable"
            if item.writes
            else "shared"
            if len(item.owners) > 1 and item.kind != "other"
            else "unresolved"
        )
        owner = next(iter(item.owners)) if kind == "private" else "shared" if kind == "shared" else kind
        name = f"rodata/{owner}/{item.address:08X}"
        providers.append(
            ProviderRecord(
                start=start,
                end=start + item.end - item.address,
                address=item.address,
                name=name,
                kind=kind,
                owners=sorted(item.owners),
                evidence={
                    **item.document(),
                    "logical_provider": "rodata/shared" if kind == "shared" else "rodata/" + owner,
                    "table_entry_bias": span.bias,
                    "sha256": hashlib.sha256(image[start : start + item.end - item.address]).hexdigest(),
                },
            )
        )
    return providers


def table_edges(image: bytes, f: split.Function, providers: list[ProviderRecord]) -> dict[int, tuple[int, ...]]:
    """Join indexed table loads to their consuming indirect branch register."""
    refs, _ = collect(f.name, image[f.start : f.end], None, None)
    result = {}
    for ref in refs:
        if ref.type != "indexed" or ref.scale != 4:
            continue
        pool = next(
            (
                p
                for p in providers
                if p["address"] == ref.address and p["evidence"].get("kind") == "jump table" and p["owners"] == [f.name]
            ),
            None,
        )
        if pool is None:
            continue
        values = tuple(
            (value + pool["evidence"]["table_entry_bias"]) & 0xFFFFFFFF
            for value in words(image[pool["start"] : pool["end"]])
        )
        if not values or any(value % 4 or not f.address <= value < f.address + f.end - f.start for value in values):
            continue
        load = int.from_bytes(image[f.start + ref.offset : f.start + ref.offset + 4], "big")
        register = load >> 16 & 31
        for offset in range(f.start + ref.offset + 4, min(f.end, f.start + ref.offset + 24), 4):
            word = int.from_bytes(image[offset : offset + 4], "big")
            if word >> 26 == 0 and word & 63 == 8 and word >> 21 & 31 == register and register != 31:
                result[offset] = tuple(value - f.address + f.start for value in values)
                break
            destination = word >> 11 & 31 if word >> 26 == 0 else word >> 16 & 31
            if destination == register:
                break
    return result


def complete_providers(
    image: bytes, ff: list[split.Function], constants: list[ProviderRecord], ranges: tuple[split.Function, ...]
) -> list[ProviderRecord]:
    providers = list(constants)
    for f in ff:
        providers.append(
            ProviderRecord(
                start=f.start,
                end=f.end,
                address=f.address,
                name=f.name,
                kind="text",
                owners=[f.name],
                evidence={"source": "pinned disassembler", "assembly": True},
            )
        )
    ordered = sorted(providers, key=lambda p: p["start"])
    result: list[ProviderRecord] = []
    cursor = 0
    for p in ordered:
        if p["start"] < cursor:
            raise Held("setup", f"layout.pool_span: overlapping provider at ROM 0x{p['start']:X}")
        if p["start"] > cursor:
            containing = next((f for f in ranges if f.start <= cursor < p["start"] <= f.end), None)
            address = containing.address + cursor - containing.start if containing else None
            result.append(
                ProviderRecord(
                    start=cursor,
                    end=p["start"],
                    address=address,
                    name=f"retained_{cursor:X}",
                    kind="unresolved" if address else "bin",
                    owners=[],
                    evidence={"classification": "unclaimed bytes retained"},
                )
            )
        result.append(p)
        cursor = p["end"]
    if cursor < len(image):
        result.append(
            ProviderRecord(
                start=cursor,
                end=len(image),
                address=None,
                name=f"retained_{cursor:X}",
                kind="bin",
                owners=[],
                evidence={"classification": "unmapped bytes retained"},
            )
        )
    merged: list[ProviderRecord] = []
    for row in result:
        if merged:
            previous = merged[-1]
            contiguous = (
                previous["end"] == row["start"]
                and previous["address"] is not None
                and row["address"] == previous["address"] + previous["end"] - previous["start"]
            )
            if contiguous and row["kind"] == previous["kind"] and row["kind"] in ("unresolved", "writable"):
                previous["evidence"].setdefault("parts", []).append(dict(row))
                previous["end"] = row["end"]
                previous["owners"] = sorted(set(previous["owners"] + row["owners"]))
                continue
        merged.append(row)
    return merged


def render_yaml(template: str, providers: list[ProviderRecord], size: int) -> str:
    """One native load selector per ROM byte, with independent pool objects."""
    prefix = template.split("segments:\n", 1)[0]
    prefix = re.sub(r"^  (?:segment_start_align|segment_end_align):.*\n", "", prefix, flags=re.M)
    prefix += "  ld_align_segment_start: 1\n  ld_align_segment_vram_end: false\n  ld_align_section_vram_end: false\n"
    lines = [prefix, "segments:\n"]
    # Keep cartridge header and IPL3 exact, independent of target symbols.
    previous: ProviderRecord | None = None
    for p in providers:
        start, address = p["start"], p["address"]
        if address is None:
            # Unmapped cartridge bytes have no RAM placement. Use their ROM
            # offsets solely for independent ELF storage, avoiding Splat's
            # implicit continuation of the preceding loaded RAM segment.
            lines.extend(
                (
                    f"  - name: {json.dumps(p['name'])}\n",
                    "    type: bin\n",
                    f"    start: 0x{start:X}\n",
                    f"    vram: 0x{start:X}\n",
                    "    align: 1\n",
                )
            )
            previous = None
            continue
        kind = "asm" if p["kind"] == "text" else "rodata" if p["kind"] in ("private", "shared") else "data"
        if (
            previous is None
            or previous["address"] is None
            or address != previous["address"] + previous["end"] - previous["start"]
        ):
            lines.extend(
                (
                    f"  - name: span_{start:X}\n",
                    "    type: code\n",
                    f"    start: 0x{start:X}\n",
                    f"    vram: 0x{address:X}\n",
                    "    align: 1\n",
                    "    subalign: 1\n",
                    "    subsegments:\n",
                )
            )
        lines.append(f"      - [0x{start:X}, {kind}, {json.dumps(p['name'])}]\n")
        previous = p
    lines.append(f"  - [0x{size:X}]\n")
    return "".join(lines)


def plan_layout(project: PendingProject, census: Census, policy: SetupPolicy) -> LayoutManifest:
    executable = shutil.which(str(policy.splat))
    if executable is None:
        raise Held("setup", "policy.splat: missing executable")
    project.build.mkdir(parents=True, exist_ok=True)
    signatures = boundary_signatures.configured() if __import__("os").environ.get("UNBAKE_BOUNDARY_SIGNATURES") else ()
    inputs = {
        **{
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__).parent / name
                for name in (
                    "planner.py",
                    "boundary.py",
                    "boundary_signatures.py",
                    "split_analysis.py",
                    "rodata_owners.py",
                    "rodata_references.py",
                    "split_create.py",
                    "split.py",
                    "xver.py",
                    "symbol_identity.py",
                )
            )
        },
        "policy": digest(asdict(policy)),
        "signatures": digest([asdict(signature) for signature in signatures]),
        "roms": hashlib.sha256(census.manifest.read_bytes()).hexdigest(),
        "splat": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
    }
    manifest_path = project.build / "setup/layout.json"
    if manifest_path.is_file():
        saved = json.loads(manifest_path.read_text())
        if (
            saved.get("inputs_sha256") == inputs
            and saved.get("project_id") == project.id
            and saved.get("workspace_id") == project.workspace_id
            and saved.get("names_from") == census.names_from
            and saved.get("rom_sha1") == {census.names[rom.path]: rom.sha1 for rom in census.cartridges}
        ):
            from typing import cast

            return cast(LayoutManifest, saved)
    inventories = {}
    measured_by_version = {}
    templates = {}
    loaded_by_version = {}
    images = {census.names[rom.path]: rom for rom in census.cartridges}
    ranges_by_version = {census.names[rom.path]: census.inventories[rom.path] for rom in census.cartridges}
    with tempfile.TemporaryDirectory(prefix=".layout-", dir=project.build) as temporary:
        root = Path(temporary)
        for cartridge in census.cartridges:
            version = census.names[cartridge.path]
            work = root / version
            work.mkdir()
            image = cartridge.image()
            input_path = work / "source.z64"
            input_path.write_bytes(image)
            template = split_create.create(
                input_path,
                "layout",
                version,
                policy=policy,
                code_ranges=[(row.start, row.end, row.address - row.start) for row in ranges_by_version[version]],
            )
            cache = project.build / "setup" / f"measurement-{version}.json"
            key = digest([cartridge.sha1, template, hashlib.sha256(Path(executable).read_bytes()).hexdigest()])
            if cache.is_file() and (saved := json.loads(cache.read_text())).get("key") == key:
                measured = split.ExtractedText(
                    [
                        split.Function(
                            **{
                                **row,
                                "aliases": tuple(row["aliases"]),
                                "entries": tuple(tuple(entry) for entry in row.get("entries", ())),
                            }
                        )
                        for row in saved["functions"]
                    ],
                    tuple(tuple(row) for row in saved["data"]),
                )
            else:
                measured = measure(image, template, Path(executable), work, version)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps(
                        {"key": key, "functions": [asdict(f) for f in measured.functions], "data": measured.data},
                        sort_keys=True,
                    )
                )
            invalid = {
                (offset, offset + 4)
                for f in measured.functions
                for offset in range(f.start, f.end, 4)
                if not split_analysis.instruction(int.from_bytes(image[offset : offset + 4], "big"))
            }
            measured = split.ExtractedText(measured.functions, tuple(sorted(set(measured.data) | invalid)))
            measured_by_version[version] = measured
            templates[version] = template
            inventories[version] = functions(image, version, ranges_by_version[version], measured)
            loaded_by_version[version] = mappings(image, ranges_by_version[version])
            del image
    identity: dict[str, dict[int, str]] = {}
    symbol_evidence: dict[str, dict[int, dict[str, Any]]] = {}
    names = correspondence(
        images,
        inventories,
        census.names_from,
        evidence=identity,
        symbol_evidence=symbol_evidence,
        loaded_spans={v: spans for v, (_, spans) in loaded_by_version.items()},
    )
    holding: dict[str, list[str]] = defaultdict(list)
    for version, placements in names.items():
        for name in placements.values():
            holding[name].append(version)
    versions: dict[str, VersionLayout] = {}
    for version, original in inventories.items():
        ff = [
            split.Function(
                f.version, names[version][f.start], f.start, f.end, f.address, names[version][f.start], f.kind, ()
            )
            for f in original
        ]
        image, ranges = images[version].image(), ranges_by_version[version]
        loaded, loaded_spans = loaded_by_version[version]
        spans = constant_spans(image, loaded, ranges, measured_by_version[version])
        previous = None
        for _iteration in range(1, 5):
            constants = carve(image, ff, spans)
            providers = complete_providers(image, ff, constants, ranges)
            current = digest(providers)
            if current == previous:
                break
            previous = current
        else:
            raise Held("setup", f"layout.plan_unstable: VERSION {version}: ownership did not converge")
        signatures = (
            boundary_signatures.configured() if __import__("os").environ.get("UNBAKE_BOUNDARY_SIGNATURES") else ()
        )
        seeds = {}
        for f in ranges:
            seeds.update(boundary.entries(image, f.start, f.end, f.address - f.start, signatures))
        records: list[FunctionRecord] = []
        for f in ff:
            body = body_identity(image, f.start, f.end)
            code = {offset: int.from_bytes(image[offset : offset + 4], "big") for offset in range(f.start, f.end, 4)}
            evidence = boundary.evidence(
                code,
                f.start,
                f.end,
                f.address - f.start,
                seeds.get(f.start, {"disassembler-entry"}),
                set(seeds),
                16,
                table_edges(image, f, constants),
            )
            records.append(
                FunctionRecord(
                    start=f.start,
                    end=f.end,
                    address=f.address,
                    name=f.name,
                    body_sha256=body["body_sha256"],
                    normalized_body_sha256=body["normalized_body_sha256"],
                    evidence={
                        "boundary": asdict(evidence),
                        "source": "pinned disassembler",
                        "assembly": True,
                        "correspondence": identity[version][f.start],
                        "symbol_correspondence": symbol_evidence[version][f.start],
                        "holding_versions": holding[f.name],
                        "name_source": holding[f.name][0],
                    },
                )
            )
        resident = [dict(start=s.start, end=s.end, address=s.address, table_entry_bias=s.bias) for s in spans]
        versions[version] = VersionLayout(
            functions=records,
            providers=providers,
            loaded_spans=loaded_spans,
            evidence={
                "split_yaml": render_yaml(templates[version], providers, len(image)),
                "symbols_text": "".join(f"{f.name} = 0x{f.address:08X}; // type:func\n" for f in ff),
                "resident_mappings": resident,
                "copy_evidence": [[row["start"], row["end"], row["address"]] for row in loaded],
                "coverage_bytes": sum(p["end"] - p["start"] for p in providers),
                "counts": dict(Counter(p["kind"] for p in providers)),
                "shared_provider": "rodata/shared",
                "iterations": _iteration,
            },
        )
        del image
    manifest = LayoutManifest(
        schema=1,
        project_id=project.id,
        workspace_id=project.workspace_id,
        rom_sha1={census.names[rom.path]: rom.sha1 for rom in census.cartridges},
        names_from=census.names_from,
        versions=versions,
        items=symbol_items(versions),
        inputs_sha256=inputs,
    )
    path = project.build / "setup/layout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def render_layout(project: PendingProject, census: Census, layout: LayoutManifest) -> dict[str, str]:
    """Supply staged split, symbol and ownership files to setup publication."""
    import tomllib

    facts = tomllib.loads((project.root / "config.toml").read_text())["project"]
    name = facts.get("name", project.root.name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise Held("setup", "project.name: supply setup --name for the artifact stem")
    files = {}
    for version, row in layout["versions"].items():
        text = row["evidence"]["split_yaml"].replace("basename: layout", "basename: " + name)
        options = {
            "target_path": (project.roms / f"baserom.{version}.z64").relative_to(project.root).as_posix(),
            "asm_path": (project.asm / version).relative_to(project.root).as_posix(),
            "src_path": project.src.relative_to(project.root).as_posix(),
            "build_path": (project.build / version).relative_to(project.root).as_posix(),
            "extensions_path": (project.tools / "splat_ext").relative_to(project.root).as_posix(),
        }
        for key, value in options.items():
            text = re.sub(rf"^  {key}:.*$", "  " + key + ": " + json.dumps(value), text, flags=re.M)
        files[f"versions/{version}/{name}.yaml"] = text
        files[f"versions/{version}/symbol_addrs.txt"] = row["evidence"]["symbols_text"]
        files[f"docs/setup/{version}.json"] = json.dumps(row, indent=2, sort_keys=True) + "\n"
    return files
