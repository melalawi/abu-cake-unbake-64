"""Compile pinned canonical sources and compare relocation-masked target code."""

from __future__ import annotations

import hashlib
import struct
import tempfile
from pathlib import Path
from typing import Any

from unbake import atomic as atomic_files
from unbake.compilers import profiles as compiler_profiles
from unbake.compilers import registry as toolchain
from unbake.config import Held, Host, PendingProject
from unbake.objects.elf import Object
from unbake.process import run_tool

CANONICAL = (
    "int identity(int x) { return x; }\n"
    "int zero(void) { return 0; }\n"
    "int load(int *p) { return *p; }\n"
    "void store(int *p,int x) { *p=x; }\n"
    "void copy(int *d,int *s,int n) { while(n--) *d++=*s++; }\n"
    "void copybyte(unsigned char *d,unsigned char *s,int n) { while(n--) *d++=*s++; }\n"
)


def exemplars(path: Path) -> list[compiler_profiles.Exemplar]:
    obj = Object(path)
    text = obj.section(".text")
    if text is None:
        raise ValueError("probe object missing .text")
    body = obj.content(text)
    symbols = sorted(
        (s for table in obj.symbols.values() for s in table if s["section"] == text and s["info"] & 15 == 2),
        key=lambda s: s["value"],
    )
    output = []
    for index, symbol in enumerate(symbols):
        start = symbol["value"]
        end = (
            start + symbol["size"]
            if symbol["size"]
            else (symbols[index + 1]["value"] if index + 1 < len(symbols) else len(body))
        )
        words = tuple(word for (word,) in struct.iter_unpack(">I", body[start:end]))
        masks = [0] * len(words)
        for offset, kind, _ in obj.relocations(text):
            if start <= offset < end:
                if kind not in (2, 4, 5, 6):
                    raise ValueError(f"unsupported probe relocation {kind}")
                masks[(offset - start) // 4] = 0xFFFFFFFF if kind == 2 else 0x03FFFFFF if kind == 4 else 0xFFFF
        output.append(compiler_profiles.Exemplar(symbol["name"], words, tuple(masks)))
    if not output:
        raise ValueError("probe object has no function symbols")
    return output


def reproduce(
    project: PendingProject, policy: Host, ids: list[str], units: list[dict[str, Any]], image: bytes
) -> dict[str, Any]:
    tables = toolchain._read(toolchain.REGISTRY_PATH)["fingerprints"]
    directory = project.build / "setup/probes"
    directory.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"attempted": 0, "successful_comparable": 0, "errors": [], "candidates": {}}
    targets = {
        unit["name"]: tuple(word for (word,) in struct.iter_unpack(">I", image[unit["start"] : unit["end"]]))
        for unit in units
    }
    for ident in ids:
        spec = toolchain.specification(ident)
        row: dict[str, Any] = {"compiler_pins": spec.pins, "cflags": list(spec.cflags), "probes": [], "matches": []}
        report["candidates"][ident] = row
        for label, source in (("calibration", tables[ident]["calibration_source"]), ("canonical", CANONICAL)):
            report["attempted"] += 1
            with tempfile.TemporaryDirectory(prefix="compile-", dir=directory) as temporary:
                work = Path(temporary)
                path = work / "probe.c"
                atomic_files.text(path, source)
                probe: dict[str, Any] = {
                    "name": label,
                    "source": source,
                    "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                }
                row["probes"].append(probe)
                try:
                    cache = policy.cache_root / "compilers" / ident
                    toolchain.verify(cache, spec)
                    if spec.kind == "ido":
                        run_tool(
                            [str(cache / spec.cc), *spec.cflags, "-c", str(path), "-o", str(work / "probe.o")],
                            work,
                            "setup",
                        )
                    else:
                        run_tool(
                            [str(cache / spec.cc), "-quiet", *spec.cflags, str(path), "-o", str(work / "probe.s")],
                            work,
                            "setup",
                        )
                        run_tool(
                            [
                                str(policy.mips_as),
                                *project.asflags,
                                *project.sn64_asflags,
                                str(work / "probe.s"),
                                "-o",
                                str(work / "probe.o"),
                            ],
                            work,
                            "setup",
                        )
                    examples = exemplars(work / "probe.o")
                except (Held, OSError, ValueError, struct.error) as error:
                    reason = str(error).replace(str(work), "<probe>")
                    probe["error"] = reason
                    report["errors"].append({"candidate": ident, "probe": label, "reason": reason})
                    continue
                report["successful_comparable"] += 1
                probe["functions"] = [
                    {
                        "name": e.name,
                        "words": [f"0x{w:08X}" for w in e.words],
                        "relocation_masks": [f"0x{m:08X}" for m in e.masks],
                    }
                    for e in examples
                ]
                for name, words in targets.items():
                    for example in examples:
                        if example.matches(words):
                            row["matches"].append(name + "/" + label + "/" + example.name)
    for ident, row in report["candidates"].items():
        row["score"] = [
            sum(
                not any(
                    match in other["matches"] for other_id, other in report["candidates"].items() if other_id != ident
                )
                for match in row["matches"]
            ),
            len(row["matches"]),
        ]
    return report
