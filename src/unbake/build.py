"""Toolchain installation, splat extraction and portable build-file rendering."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tarfile
import urllib.request
from bisect import bisect_right
from pathlib import Path
from string import Template
from typing import cast

import yaml

from unbake import adapters, effort, native, pool, process, recipes, store, symbols, view
from unbake import config as configuration
from unbake.contracts import Config, Finding, Json, NativeResult, Recipe, Refusal, Snapshot, UnitSpec, digest

_REPO = configuration.load_resource("repo.toml")
_WORD = 0xFFFFFFFF
_TAG = "@RULES@"  # stands for the digest of the rule it is in: a changed rule has outputs of its own
_MAKE = {row["host"]: f"$({var})" for var, row in _REPO["make"]["tools"].items()}
def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
def _pins_bad(dest: Path, pins: Json) -> list[str]:
    """Pinned files that are missing or hash wrong. A hash is remembered with the file's size and mtime."""
    manifest = dest / ".pin-manifest.json"
    try:
        known = json.loads(manifest.read_text())
    except (OSError, ValueError):
        known = {}
    bad, fresh = [], {}
    for rel, want in sorted(pins.items()):
        path = dest / rel
        if not path.is_file():
            bad.append(rel)
            continue
        stat, row = path.stat(), known.get(rel)
        have = row[2] if row and row[:2] == [stat.st_mtime_ns, stat.st_size] else _file_hash(path)
        fresh[rel] = [stat.st_mtime_ns, stat.st_size, have]
        if have != want:
            bad.append(rel)
    if fresh != known and dest.is_dir():
        manifest.write_text(json.dumps(fresh))
    return bad
def _fetch(config: Config, row: Json) -> Path:
    cached = config.host.toolchain_root / ".downloads" / row["sha256"]
    if cached.is_file() and _file_hash(cached) == row["sha256"]:
        return cached
    try:
        with urllib.request.urlopen(row["url"]) as response:
            data = response.read()
    except OSError as error:
        raise Refusal(Finding("toolchain.download", reason=f"{row['url']}: {error}", path=row["url"])) from error
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise Refusal(Finding("toolchain.download", reason=f"{row['url']} does not match sha256 {row['sha256']}",
                              path=row["url"]))
    store.write(cached, data)
    return cached
def install_toolchain(config: Config, id: str) -> Path:
    with effort.stage("build.install_toolchain"):
        rows = configuration.load_resource("toolchains.toml")["toolchain"]
        if id not in rows:
            raise Refusal(Finding("toolchain.pin", reason=f"toolchain {id} is not in toolchains.toml"))
        row, dest = rows[id], config.host.toolchain_root / id
        if not _pins_bad(dest, row["pins"]):
            return dest
        for download in row["downloads"]:
            with tarfile.open(_fetch(config, download)) as archive:
                names = set(archive.getnames())
                absent = [f for f in download["files"] if f not in names]
                if absent:
                    raise Refusal(Finding("toolchain.download", reason=f"{download['url']} lacks {absent}",
                                          path=download["url"], missing=tuple(absent)))
                dest.mkdir(parents=True, exist_ok=True)
                archive.extractall(dest, members=[archive.getmember(f) for f in download["files"]], filter="data")
            for member in download["files"]:
                path = dest / member
                if path.is_file():
                    path.chmod(path.stat().st_mode | 0o111)
        bad = _pins_bad(dest, row["pins"])
        if bad:
            raise Refusal(Finding("toolchain.pin", reason=f"{id}: {', '.join(bad)} do not match their pins",
                                  path=str(dest), missing=tuple(bad)))
        return dest
def _shell(argv) -> str:
    return " ".join(shlex.quote(str(w)) for w in argv)
def _values(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe) -> dict[str, list[str]]:
    project = snapshot.config.project
    key = recipe.toolchain.replace("-", "_").replace(".", "_")
    macros = _REPO["splat"]["options"]["generated_asm_macros_directory"].format(version=version, name=project.name)
    return {**{k: [v] for k, v in _MAKE.items()}, "cc": [f"$(CC_{key})"], "as": [f"$(AS_{key})"],
            "cppflags": list(recipe.cppflags), "codegen": list(recipe.cflags),
            "asflags": list(recipe.asflags), "defines": list(project.version_macros[version]),
            "includes": ["-Iinclude", f"-I{Path(unit.path).parent}", f"-I{macros}"], "name": [Path(unit.path).stem]}
def _reading(snapshot: Snapshot, unit: UnitSpec, version: str) -> list[str]:
    """The command line that reads the unit's source, whose search flags decide which headers it reaches."""
    recipe = recipes.resolve(snapshot.config, unit, {})
    row = configuration.load_resource("toolchains.toml")["toolchain"][recipe.toolchain]
    phases = configuration.load_resource("units.toml")["kind"][unit.kind]["phases"]
    values = {**_values(snapshot, unit, version, recipe), "source": [unit.path], "out": ["-"]}
    if "preprocess" in phases:
        return adapters.render(row["preprocess"], values)
    if "assemble" in phases:
        return adapters.render(adapters.assemble_template(row if "compile" in phases else None)[0], values)
    return []  # armips: only file-relative includes, and an unfound one is covered by the caller
def _headers(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    def scan() -> tuple[tuple[str, ...], tuple[str, ...]]:
        return view.headers(snapshot, unit, _reading(snapshot, unit, version))
    return cast("tuple[tuple[str, ...], tuple[str, ...]]",
                effort.memo(("unit-headers", snapshot.digest, unit.path, version), scan))
def _versions(snapshot: Snapshot, unit: UnitSpec) -> list[str]:
    return sorted(snapshot.layout.held(unit.members) - set(unit.withheld))
def _unit_rules(snapshot: Snapshot, unit: UnitSpec, version: str) -> str:
    """The rules of one unit in one version. Their outputs live in a directory named by the digest of everything the
    recipe says, so a changed recipe has no outputs yet and the rules need not depend on the Makefile."""
    cfg, project = snapshot.config, snapshot.config.project
    recipe = recipes.resolve(cfg, unit, {})
    row = configuration.load_resource("toolchains.toml")["toolchain"][recipe.toolchain]
    kind = configuration.load_resource("units.toml")["kind"][unit.kind]
    phases = kind["phases"]
    home = Path("build") / version / Path(unit.path).with_suffix("")
    work = home / _TAG
    obj, current = work / "source.o", unit.path
    commands = [f"mkdir -p {shlex.quote(str(work))}",
                f"find {shlex.quote(str(home))} -mindepth 1 -maxdepth 1 ! -name {_TAG} -exec rm -rf {{}} +"]
    key = recipe.toolchain.replace("-", "_").replace(".", "_")
    values = _values(snapshot, unit, version, recipe)
    for phase in phases:
        if phase == "preprocess":
            out = work / "source.i"
            argv = adapters.render(row[phase], {**values, "source": [str(current)], "out": [str(out)]})
            commands.append(_shell(argv) + f" > {out} 2> {work}/preprocess.err")
            if row.get("error_pattern"):
                commands.append(f"! grep -Eq {shlex.quote(row['error_pattern'])} {work}/preprocess.err")
        elif phase == "compile":
            out = work / ("source.s" if row["emits_asm"] else "source.o")
            argv = adapters.render(row[phase], {**values, "source": [str(current)], "out": [str(out)]})
            commands.append(_shell(argv) + f" 2> {work}/compile.err || {{ cat {work}/compile.err >&2; exit 1; }}")
        elif phase == "assemble":
            if "compile" in phases and not row["emits_asm"]:
                continue
            out = obj
            template, tool = adapters.assemble_template(row if "compile" in phases else None)
            commands.append(_shell(adapters.render(template, {**values, "as": values[tool],
                                   "source": [str(current)], "out": [str(out)]})))
        elif phase == "armips":
            out, wrapper = work / "data.bin", work / "resource.s"
            commands.append(f"printf '%s\\n' '.create \"{out}\", 0' > {wrapper}")
            commands.append(f"sed -E '/^[[:space:]]*\\.(create|close)([[:space:]]|$)/d' {unit.path} >> {wrapper}")
            commands.append(f"printf '%s\\n' '.close' >> {wrapper}")
            commands.append(_shell([_MAKE["armips"], wrapper, "-root", "."]))
        else:
            continue
        current = out
    ranges = native.sections(snapshot, unit, version)
    outputs, patches = [], []
    if "armips" in phases:
        if len(ranges) != 1 or ranges[0].section == ".bss":
            raise Refusal(Finding("link.error", "resource must own one ROM section", unit=unit.path))
        outputs.append(str(current))
        patches.append(f"{current}:{ranges[0].rom_start}")
        commands.append(f'test "$$(wc -c < {current})" -eq {ranges[0].size}')
    else:
        symbols = Path("versions") / version / "symbols.ld"
        trim = not row["preserves_padding"] or snapshot.layout.members[unit.members[0]].state == "hasm"
        script = adapters.link_script(ranges, symbols, trim)
        commands.append("printf '%s\\n' " + _shell(script.splitlines()) + f" > {work}/unit.ld")
        commands.extend(_shell(argv) for argv in adapters.link_commands(_MAKE, obj, ranges, work, trim))
        for placement in ranges:
            suffix = ".size" if placement.section == ".bss" else ".bin"
            output = str(work / (placement.section[1:] + suffix))
            outputs.append(output)
            if placement.section == ".bss":
                commands.append(f"printf '%s' '{placement.size}' > {output}")
            else:
                commands.append(f'test "$$(wc -c < {output})" -eq {placement.size}')
                patches.append(f"{output}:{placement.rom_start}")
    found, missing = _headers(snapshot, unit, version)
    dependencies = [unit.path, f"versions/{version}/symbols.ld", *found]
    if missing:  # a quoted include found nowhere yet: wherever it appears, and any project header, may be it
        dependencies.append("$(wildcard " + " ".join(missing) + " include/*.h include/*/*.h)")
    if "compile" in phases:
        dependencies.append(f"$(TOOLCHAIN_{key})")
    text = Template(configuration.template("unit.mk.in")).substitute(unit=unit.path, version=version,
        outputs=" ".join(outputs), dependencies=" ".join(dependencies),
        commands=" && ".join(commands), rom=f"build/{version}/{project.name}.z64", patches=" ".join(patches))
    return text.replace(_TAG, digest(text)[:12])
_CODE = digest([Path(m.__file__).read_bytes() for m in (adapters, native, recipes)] + [Path(__file__).read_bytes()])
def _unit_key(item) -> str:
    """A unit's rules read its own rows, text, headers and recipe, never another unit's: one changed unit, one key."""
    snapshot, unit = item
    context = effort.memo(("unit-rules", snapshot.config.digest), lambda: digest((
        snapshot.config.project, configuration.load_resource("toolchains.toml"),
        configuration.load_resource("units.toml"), configuration.template("unit.mk.in"), _CODE)))
    headers = [_headers(snapshot, unit, v) for v in _versions(snapshot, unit)]
    return digest((unit, [snapshot.layout.members[n] for n in unit.members], headers, context))
def _unit_job(item) -> list[str]:
    snapshot, unit = item
    return [_unit_rules(snapshot, unit, v) for v in _versions(snapshot, unit)]
def makefile(snapshot: Snapshot) -> bytes:
    with effort.stage("build.makefile"):
        cfg, project = snapshot.config, snapshot.config.project
        rows = configuration.load_resource("toolchains.toml")["toolchain"]
        tools = [f"ifneq ($(filter default undefined,$(origin {var})),)\n{var} := {row['default']}\nendif"
                 for var, row in sorted(_REPO["make"]["tools"].items())]  # make's own CPP and AS beat a ?= line
        blocks, chains, rom_rules, checks = [], [], [], []
        for id in sorted({u.toolchain for u in snapshot.layout.units.values()} | {project.toolchain}):
            key, row = id.replace("-", "_").replace(".", "_"), rows[id]
            n = len(row["downloads"])
            if n > 1:
                raise Refusal(Finding("toolchain.download",
                                      f"{id} has {n} downloads; the generated build supports one", path=id))
            tools.append(f"CC_{key} ?= tools/{id}/{row['cc']}" if n else
                         f"CC_{key} ?= $(error CC_{key} must be set in local.mk)")
            tools.append(f"AS_{key} ?= tools/{id}/{row['as']}" if "as" in row else f"AS_{key} ?= $(AS)")
            tools.append(f"TOOLCHAIN_{key} ?= tools/{id}/.stamp" if n else f"TOOLCHAIN_{key} ?=")
            if n:
                download = row["downloads"][0]
                chains.append(Template(configuration.template("toolchain.mk.in")).substitute(
                    stamp=f"tools/{id}/.stamp", dir=f"tools/{id}", url=download["url"], archive=f"tools/{id}.tar.gz",
                    sha256=download["sha256"], files=" ".join(download["files"]), stamp_name=".stamp",
                    pins=" ".join(f"'{sha}  {file}'" for file, sha in sorted(row["pins"].items()))))
        units = sorted(snapshot.layout.units.values(), key=lambda u: u.path)
        for fragments in pool.map(cfg, "build.units", _unit_job, [(snapshot, u) for u in units], _unit_key):
            blocks.extend(fragments)
        for version in project.versions:
            files, rom = project.version_files[version], f"build/{version}/{project.name}.z64"
            checks.append(f'\t$(Q)test "$$(sha1sum < {rom} | cut -d\' \' -f1)" = {files.baserom_sha1}')
            rom_rules.append(f"{rom}: {files.baserom} Makefile\n"
                f"\t$(Q)mkdir -p $(@D) && cp {shlex.quote(files.baserom)} $@ && "
                'for p in $(PATCHES); do dd if=$${p%:*} of=$@ bs=64K oflag=seek_bytes seek=$${p##*:} '
                'conv=notrunc status=none || exit 1; done\n'
                f"\t$(Q)test \"$$({{ sha1sum < $@; }} | cut -d' ' -f1)\" = {files.baserom_sha1}\n")
        return Template(configuration.template("Makefile.in")).substitute(
            title=project.title, name=project.name, versions=" ".join(project.versions),
            tool_defaults="\n".join(tools), toolchain_rules="\n".join(chains), units="\n".join(blocks),
            tree_checks="\n".join(f"\t$(Q)test -s versions/{v}/report.json" for v in project.versions),
            roms=" ".join(f"build/{v}/{project.name}.z64" for v in project.versions),
            rom_checks="\n".join(checks), rom_rules="\n".join(rom_rules)).encode()
def local_mk(config: Config) -> bytes:
    with effort.stage("build.local_mk"):
        assignments = [f"JOBS := {config.host.workers}", *(f"{var} := {config.host.tools.get(row['host'], '')}"
                                                           for var, row in sorted(_REPO["make"]["tools"].items()))]
        for id, row in sorted(configuration.load_resource("toolchains.toml")["toolchain"].items()):
            key, base = id.replace("-", "_").replace(".", "_"), config.host.toolchain_root / id
            assignments.append(f"CC_{key} := {base / row['cc']}")
            assignments.append(f"AS_{key} := {base / row['as']}" if "as" in row else f"AS_{key} := $(AS)")
            assignments.append(f"TOOLCHAIN_{key} :=")
        return Template(configuration.template("local.mk.in")).substitute(assignments="\n".join(assignments)).encode()
def symbols_ld(snapshot: Snapshot, version: str) -> bytes:
    with effort.stage("build.symbols_ld"):
        rows, mask = snapshot.versions[version].symbols, snapshot.config.project.build.get("text_mask", _WORD)
        if mask != _WORD:  # the code is linked at its masked address, so a symbol in it is provided there too
            spans = _text_spans(snapshot, version)
            table = symbols.load(snapshot.read, snapshot.config.project.versions, store.content(snapshot.config).cached)
            code = snapshot.versions[version].code
            rows = {n: a & mask if n in code or table.get(n, {}).get("kind") == "function"
                    or spans[bisect_right(spans, (a, _WORD)) - 1][1] > a else a for n, a in rows.items()}
        return "".join(f"PROVIDE({name} = 0x{rows[name]:08X});\n" for name in sorted(rows)).encode("utf-8")
def _text_spans(snapshot: Snapshot, version: str) -> list[tuple[int, int]]:
    """The disjoint (start, end) vram runs that text rows cover, sorted, with a sentinel before the first."""
    def spans() -> list[tuple[int, int]]:
        merged = [(-1, 0)]
        for start, end in sorted((p.vram, p.vram + p.rom_end - p.rom_start) for m in snapshot.layout.members.values()
                                 for p in m.placements if p.version == version and p.section == ".text"):
            merged.append((start, end)) if start > merged[-1][1] else merged.__setitem__(
                -1, (merged[-1][0], max(end, merged[-1][1])))
        return merged
    return effort.memo(("spans", snapshot.digest, version), spans)
def _split_input(text: str):
    document, changed = yaml.safe_load(text), False
    for segment in document["segments"]:
        if not isinstance(segment, dict):
            continue
        for row in [segment, *segment.get("subsegments", [])]:
            key = "name" if isinstance(row, dict) else 2
            if (isinstance(row, dict) and key not in row) or (isinstance(row, list) and len(row) < 3):
                continue
            name = row[key]
            if not isinstance(name, str):
                continue
            bounded = "/".join(part if len(part.encode()) <= 240 else
                               part.encode()[:180].decode(errors="ignore") + "_" +
                               hashlib.sha256(part.encode()).hexdigest()[:16] for part in name.split("/"))
            if bounded != name:
                row[key], changed = bounded, True
    return document if changed else None
_READ_PATHS = ("target_path", "symbol_addrs_path", "reloc_addrs_path", "extensions_path")
_UNTOUCHED = ("base_path", "data_path", "nonmatchings_path", "matchings_path")
def _publish(stage: Path, root: Path, owned: Path) -> None:
    """Copy what splat wrote under stage into the project where the bytes differ, so an unchanged file keeps its
    mtime and make rebuilds only what changed. Files of the version's asm tree that the run did not write are removed.
    The stage path splat spelled inside a file is the project path."""
    old, new, written = str(stage).encode() + b"/", str(root).encode() + b"/", set()
    for source in stage.rglob("*"):
        if not source.is_file():
            continue
        target = root / source.relative_to(stage)
        written.add(target)
        store.write(target, source.read_bytes().replace(old, new))
    for stale in (p for p in (root / owned).rglob("*") if p.is_file() and p not in written):
        stale.unlink()
def extract(config: Config, version: str) -> NativeResult:
    with effort.stage("build.extract"):
        root = config.project.root
        split = root / config.project.version_files[version].split
        document = _split_input(split.read_text()) or yaml.safe_load(split.read_text())
        with store.work(config) as work:
            stage = work / "tree"
            options = document.setdefault("options", {})
            defaults = dict(asm_path="asm", src_path="src", asset_path="assets", build_path="build",
                            lib_path="lib", o_path="build", generated_asm_macros_directory="include",
                            symbol_addrs_path="symbol_addrs.txt", reloc_addrs_path="reloc_addrs.txt",
                            undefined_funcs_auto_path="undefined_funcs_auto.txt",
                            undefined_syms_auto_path="undefined_syms_auto.txt",
                            ld_script_path=f"{options.get('basename', 'rom')}.ld")
            for key, value in defaults.items():
                options.setdefault(key, value)
            for key, value in list(options.items()):
                if (key.endswith("_path") and key not in _UNTOUCHED) or key == "generated_asm_macros_directory":
                    home = root if key in _READ_PATHS else stage
                    options[key] = [str(home / p) for p in value] if isinstance(value, list) else str(home / value)
            options.update(base_path=str(work), cache_path=str(work / ".splache"), dump_symbols=True)
            private = work / "split.yaml"
            private.write_text(yaml.safe_dump(document, sort_keys=False))
            result = process.run("splat", [process.tool(config, "splat"), "split", str(private),
                                         "--make-full-disasm-for-code"], root,
                                 tmp=process.scratch(root))
            if result.exit == 0:
                _publish(stage, root, Path(options["asm_path"]).relative_to(stage))
                facts = root / ".unbake" / "symbols" / version
                facts.mkdir(parents=True, exist_ok=True)
                for path in facts.glob("*.csv"):
                    path.unlink()
                for path in (work / ".splat").glob("*.csv"):
                    shutil.copyfile(path, facts / path.name)
        if result.exit != 0:
            tail = result.stderr.decode(errors="replace").splitlines()
            raise Refusal(Finding(
                "setup.splat", versions=(version,), reason="\n".join(tail[-20:]) or f"exit {result.exit}" ))
        return result
def inputs(config: Config) -> str:
    """What `make check` reads or builds, by name, size and mtime: while it is unchanged the last check still holds."""
    root, project = str(config.project.root), config.project
    roms = (f"build/{v}/{project.name}.z64" for v in project.versions)
    paths = [f"{root}/{p}" for p in ("Makefile", "local.mk", *roms)]
    for place in ("src", "include", "asm", "versions", "tools"):
        paths += [f"{top}/{name}" for top, _, names in os.walk(f"{root}/{place}") for name in names]
    rows = []
    for path in sorted(paths):
        try:
            stat = os.stat(path)
            rows.append((path, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            rows.append((path, 0, 0))
    return hashlib.sha256(repr(rows).encode()).hexdigest()
def make_check(config: Config) -> Json:
    with effort.stage("build.make_check"):
        stamp, known = config.project.root / ".unbake/make.json", inputs(config)
        done = {"exit": 0, "versions": list(config.project.versions)}
        if stamp.exists() and json.loads(stamp.read_bytes()).get("inputs") == known:
            effort.count("make-check", True)
            return done
        effort.count("make-check", False)
        argv = [str(process.tool(config, "make")), "-k", f"-j{config.host.workers}"]
        root = config.project.root
        result = process.run("make check", [*argv, "check"], root, tmp=process.scratch(root))
        if result.exit == 0:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(json.dumps({"inputs": inputs(config)}))
            return done
        lines = result.stderr.decode(errors="replace").splitlines()
        failed = [line for line in lines if "***" in line]  # make -k ran everything it could: every failed target
        raise Refusal(Finding("setup.make", "\n".join(failed or lines[-20:]), missing=(f"make exit {result.exit}",)))
