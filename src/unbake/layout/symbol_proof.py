"""Prove name-only changes with retained objects and an ordinary cartridge link."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

from unbake.layout import port, split
from unbake.match import relink
from unbake.project import build, compiler_files, setup
from unbake.project.config import Held, Project, SetupPolicy
from unbake.project_tools.elf import Object


def available(project: Project, replacements: dict[str, str]) -> bool:
    """Unproved or edited executable inputs require the full proof instead."""
    headers = [p for root in project.include for p in root.rglob("*.h")]
    for source in project.src.rglob("*.c"):
        if source.stem in replacements:
            return False
        # An identifier in authored C needs compilation, even in inactive code.
        if set(re.findall(r"\b[A-Za-z_]\w*\b", source.read_text())) & replacements.keys():
            return False
    for version in project.versions:
        generation = project.build_link(version)
        graph = generation / ".split.mk"
        image = generation / f"{project.name}.{version}.z64"
        if not graph.is_file() or not image.is_file():
            return False
        configured = project.version(version)
        from unbake.project_tools.extract import unit_ranges

        if json.loads((generation / "unit-ranges.json").read_text()) != unit_ranges(configured.split.read_text()):
            return False
        addresses = {
            fields[0]: int(fields[1], 0)
            for line in (generation / "symbol-addresses.txt").read_text().splitlines()
            if len(fields := line.split()) >= 2
        }
        if any(addresses.get(name) != row[0] for name, row in split.symbols(configured.symbols)[1].items()):
            return False
        with image.open("rb") as stream:
            if hashlib.file_digest(stream, "sha1").hexdigest() != configured.baserom_sha1:
                return False
        c = re.search(r"^C_OBJECTS := (.*)$", graph.read_text(), re.M)
        expected = {"$(BUILD)/obj/src/" + f.path + ".o" for f in port.functions(project, version) if f.kind == "c"}
        if c is None or set(c[1].split()) != expected:
            return False
        for name in expected:
            receipt = generation / name.removeprefix("$(BUILD)/")
            receipt = receipt.with_suffix(".built")
            source = project.src / (name.removeprefix("$(BUILD)/obj/src/").removesuffix(".o") + ".c")
            if not receipt.is_file() or any(
                p.stat().st_mtime_ns > receipt.stat().st_mtime_ns for p in [source, *headers]
            ):
                return False
    return True


@lru_cache(maxsize=8)
def binding_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(name) for name in names) + r")\b")


def generated(text: str, replacements: dict[str, str]) -> str:
    """Generated JSON, paths and assembly strings contain binding identifiers too."""
    if not replacements:
        return text
    return binding_pattern(tuple(replacements)).sub(lambda m: replacements[m[0]], text)


def rebind(path: Path, replacements: dict[str, str]) -> bool:
    """Change only ELF symbol names; keep sections, values and relocations intact."""
    obj = Object(path)
    touched = False
    for index, symbols in obj.symbols.items():
        if not any(symbol["name"] in replacements for symbol in symbols):
            continue
        strings_index = obj.sections[index][6]
        strings = bytearray(obj.content(strings_index))
        offsets = {}
        for symbol in symbols:
            old = symbol["name"]
            if old in replacements:
                new = replacements[old]
                if new not in offsets:
                    offsets[new] = len(strings)
                    strings.extend(new.encode() + b"\0")
                struct.pack_into(">I", obj.data, obj.sections[index][4] + symbol["index"] * 16, offsets[new])
                touched = True
        obj.sections[strings_index][4:6] = [len(obj.data), len(strings)]
        obj.data.extend(strings)
        struct.pack_into(">10I", obj.data, obj.table + strings_index * 40, *obj.sections[strings_index])
    if touched:
        compiler_files.atomic_bytes(path, bytes(obj.data))
    return touched


def prove(
    project: Project, staged: Project, policy: SetupPolicy, replacements: dict[str, str]
) -> tuple[list[str], list[Path], dict[str, str | None]]:
    """Pin generations briefly, then transform and link outside the project lock."""
    assembly = []
    receipts = []
    with ExitStack() as pins:
        with build.lock(project):
            generations = setup._generations(project, project.versions)
            current = {v: pins.enter_context(build.pin(build.current_generation(project, v))) for v in project.versions}

        def prove_version(version: str, source: Path) -> tuple[str, list[Path]]:
            assembly = []
            destination = staged.build / (version + ".0")
            destination.parent.mkdir(parents=True, exist_ok=True)

            graph = (source / ".split.mk").read_text()
            inventory = {
                word.removeprefix("$(BUILD)/")
                for match in re.finditer(r"^(?:C|ASM|ASSET)_OBJECTS := (.*)$", graph, re.M)
                for word in match[1].split()
            }
            destination.mkdir()
            # Only active object files and link bindings are needed for this
            # proof. Old dependency receipts and obsolete providers are cache
            # history, not inputs to the retained link.
            for parent in {Path(name).parent for name in inventory}:
                (destination / parent).mkdir(parents=True, exist_ok=True)
            for name in inventory:
                original, copied = source / name, destination / name
                if name.startswith("obj/src/"):
                    shutil.copy2(original, copied)
                    for suffix in (".d", ".built"):
                        dependency = original.with_suffix(suffix)
                        if dependency.is_file():
                            shutil.copy2(dependency, copied.with_suffix(suffix))
                else:
                    os.link(original, copied)
            for path in source.iterdir():
                if path.is_file() and path.suffix in {".mk", ".ld", ".json", ".txt", ".csv", ".flags"}:
                    shutil.copy2(path, destination / path.name)
            staged.build_link(version).symlink_to(destination.name)
            pattern = re.compile(
                rb"(?<![A-Za-z0-9_])(?:"
                + b"|".join(re.escape(name.encode()) for name in replacements)
                + rb")(?![A-Za-z0-9_])"
            )
            rebound = 0
            for directory_name, _, files in os.walk(destination):
                for name in files:
                    path = Path(directory_name) / name
                    if not pattern.search(path.read_bytes()):
                        continue
                    if path.suffix == ".o":
                        rebound += rebind(path, replacements)
                    elif path.suffix in {".mk", ".d", ".ld", ".json", ".txt", ".csv", ".flags"}:
                        compiler_files.atomic_bytes(path, generated(path.read_text(), replacements).encode())
            # Publish only the active generated assembly whose bindings change;
            # cloning every stale assembly cache would dominate a name proof.
            from unbake.layout.symbol_replan import rewrite

            for name in inventory:
                if not name.startswith("obj/asm/"):
                    continue
                relative = Path(name.removeprefix("obj/asm/")).with_suffix(".s")
                original = project.asm / version / relative
                new_relative = Path(generated(str(relative), replacements))
                content = original.read_text()
                changed = rewrite(content, replacements) if pattern.search(content.encode()) else content
                if changed != content or relative != new_relative:
                    target = staged.asm / version / new_relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(changed)
                    assembly.append(target)
            # Move only explicit paths containing a changed item. Assembly cache
            # paths can include a private-provider directory as well as a stem.
            for name in inventory:
                changed = generated(name, replacements)
                if changed != name:
                    path, target = destination / name, destination / changed
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path.rename(target)
                    for suffix in (".d", ".built"):
                        dependency = path.with_suffix(suffix)
                        if dependency.is_file():
                            dependency.rename(target.with_suffix(suffix))
            c_objects = destination / "obj/src"
            # Verify the transformed graph against the canonical split/symbol
            # inputs before reusing objects; bytes and boundaries stay fixed.
            from unbake.project_tools.extract import unit_ranges

            ranges = json.loads((destination / "unit-ranges.json").read_text())
            if ranges != unit_ranges(staged.version(version).split.read_text()):
                raise Held("split", f"split.join.bindings: {version}: generated C ranges differ")
            addresses = {
                fields[0]: int(fields[1], 0)
                for line in (destination / "symbol-addresses.txt").read_text().splitlines()
                if len(fields := line.split()) >= 2
            }
            graph = (destination / ".split.mk").read_text()
            inventory = {
                word for match in re.finditer(r"^(?:C|ASM)_OBJECTS := (.*)$", graph, re.M) for word in match[1].split()
            }
            for function in port.functions(staged, version):
                kind = "src" if function.kind == "c" else "asm"
                if (
                    addresses.get(function.name) != function.address
                    or ("$(BUILD)/obj/" + kind + "/" + function.path + ".o") not in inventory
                ):
                    raise Held("split", f"split.join.bindings: {version}:{function.name}: generated binding differs")
            # Allocated object bytes, unit ranges and addresses are unchanged;
            # the transformed existing link script retains proved literal
            # placement. Only ELF symbol strings have been rebound.
            result = relink.prove(staged, policy, version, destination, {}, extracted=True, placed=True)
            if not result.ok:
                raise Held("split", f"split.join.relink: {version}: {result.log.read_text()[-6000:]}")
            # Cached objects have just been proved with these generated inputs.
            # Keep make's receipts newer than the renamed bindings/headers.
            if c_objects.is_dir():
                for path in c_objects.rglob("*.built"):
                    cached_content = path.read_bytes()
                    path.unlink()
                    path.write_bytes(cached_content)
            for name in (".split.mk", ".split"):
                path = destination / name
                receipt_content = path.read_bytes() if path.is_file() else b""
                path.unlink(missing_ok=True)
                path.write_bytes(receipt_content)
            return (
                f"{version}: name-only proof; SHA1 OK; {rebound} object symbol tables rebound; no compilation",
                assembly,
            )

        # Retained links are small; two workers share IO without multiplying
        # resident cartridge/disassembler work or holding the publication lock.
        with ThreadPoolExecutor(max_workers=min(2, policy.cores, len(current))) as executor:
            futures = {v: executor.submit(prove_version, v, source) for v, source in current.items()}
            for version in project.versions:
                line, changed = futures[version].result()
                receipts.append(line)
                assembly.extend(changed)
    return receipts, assembly, generations
