"""ROM validation, repository bootstrap and the setup spine."""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from pathlib import Path
from string import Template

import rabbitizer
import yaml

from unbake import (
    adapters,
    build,
    effort,
    infer,
    journal,
    layout,
    native,
    pool,
    process,
    recipes,
    report,
    store,
    symbols,
    types,
    versions,
    view,
)
from unbake import config as configuration
from unbake.contracts import Config, Finding, Json, Plan, Refusal, Snapshot, digest

_CODE = digest([Path(m.__file__).read_bytes() for m in (native, adapters, build, recipes, versions, view)])
def _mask(word: int) -> int:
    opcode = rabbitizer.Instruction(word).getOpcodeName()
    if opcode in {"lui", "addiu", "ori", "lw", "sw", "lh", "sh", "lb", "sb", "lwc1", "swc1"}:
        return word & 0xFFFF0000
    return word & 0xFC000000 if opcode == "jal" else word
def _windows(data: bytes, row: Json) -> set[tuple[int, ...]]:
    start, length, window = row["start"], row["length"], row["window"]
    return {tuple(_mask(int.from_bytes(chunk[j:j + 4], "big")) for j in range(0, len(chunk), 4))
            for i in range(start, min(start + length, len(data)), window)
            if len(chunk := data[i:min(i + window, start + length)]) == window}
def validate_roms(roms: Mapping[str, Path], names_from: str) -> Json:
    with effort.stage("repo.validate_roms"):
        rules = configuration.load_resource("rom.toml")
        data, result, orders, duplicates, mismatches, hashes = {}, {}, [], [], [], {}
        for v, path in sorted(roms.items()):
            try:
                data[v] = path.read_bytes()
            except OSError as error:
                raise Refusal(Finding("init.request", str(error), path=str(path))) from error
            word = int.from_bytes(data[v][:4], "big")
            if word != rules["order"]["z64"]:
                name = next((r["name"] for r in rules["order"]["refuse"] if r["word"] == word), "an unknown order")
                orders.append(Finding("init.rom_order",
                    f"{v}: first word 0x{word:08X} is {name}; convert the dump to big-endian .z64", path=str(path)))
            sha1 = hashlib.sha1(data[v]).hexdigest()
            if sha1 in hashes:
                duplicates.append(Finding("init.rom_duplicate",
                    f"{hashes[sha1]} and {v} have identical ROM bytes", versions=(hashes[sha1], v)))
            hashes[sha1] = v
            fields = {}
            for row in rules["field"]:
                raw = data[v][row["offset"]:row["offset"] + row["size"]]
                fields[row["name"]] = (raw.decode("ascii", "replace").rstrip("\x00 ")
                                       if row["encoding"] == "ascii" else raw.hex())
            result[v] = {"sha1": sha1, "fields": fields, "similarity": 1.0}
        for findings in (orders, duplicates):
            if findings:
                raise Refusal(*findings)
        if names_from not in result:
            raise Refusal(Finding("init.request", f"{names_from} is not one of the roms"))
        reference = _windows(data[names_from], rules["similarity"])
        for v in result:
            if v == names_from:
                continue
            for row in rules["field"]:
                field = row["name"]
                value, ref = result[v]["fields"][field], result[names_from]["fields"][field]
                if row["equal"] and value != ref:
                    mismatches.append(Finding("init.rom_mismatch",
                        f"{v}: {field} {value!r} differs from {names_from} {ref!r}", versions=(v,)))
            similarity = len(reference & _windows(data[v], rules["similarity"])) / len(reference) if reference else 0.0
            result[v]["similarity"] = similarity
            if similarity < rules["similarity"]["min"]:
                mismatches.append(Finding("init.rom_mismatch",
                    f"{v}: similarity {similarity:.6f} to {names_from} is below {rules['similarity']['min']}",
                    versions=(v,)))
        if mismatches:
            raise Refusal(*mismatches)
        return result
def _render(template_name: str, **values: object) -> bytes:
    return Template(configuration.template(template_name)).substitute(values).encode()
_EMPTY_TYPES = b"schema = 1\n\n[function]\n\n[global]\n\n[struct]\n"
def _extras(rules: Json, title: str, versions: object) -> dict[str, bytes]:
    roms = "\n".join(f"- `roms/baserom.{v}.z64`" for v in versions)
    return {"CONTRIBUTING.md": _render("CONTRIBUTING.md.in", title=title, roms=roms), "types.toml": _EMPTY_TYPES,
            **{row["path"]: configuration.template(row["template"]).encode() for row in rules["sdk"]}}
_write = store.write
def _checked(result, key: str, name: str) -> None:
    if result.exit != 0 or result.signal is not None:
        tail = result.stderr.decode(errors="replace").splitlines()
        raise Refusal(Finding(key, f"{name}: {tail[-1] if tail else f'exit {result.exit}, signal {result.signal}'}"))
def init(params: Json) -> Json:
    with effort.stage("repo.init"):
        root, name, roms = Path(params["dir"]).absolute(), params["name"], {}
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise Refusal(Finding("init.request", f"{root} is not an empty directory", path=str(root)))
        for item in params["rom"]:
            v, sep, path = item.partition("=")
            if not sep or not v or not path or v in roms or not Path(path).is_file():
                raise Refusal(Finding("init.request", f"--rom {item} is not a unique VERSION=PATH naming a file"))
            roms[v] = Path(path)
        toolchains = configuration.load_resource("toolchains.toml")["toolchain"]
        if params["names_from"] not in roms or params["toolchain"] not in toolchains:
            raise Refusal(Finding("init.request", "names_from must name a ROM and toolchain must name a toolchain"))
        host = configuration.load_host(Path(params["host"]))
        versions, rules = validate_roms(roms, params["names_from"]), configuration.load_resource("repo.toml")
        created, blocks = [], ["[resident]\n"]
        def write(path: str, value: bytes) -> None:
            _write(root / path, value)
            created.append(path)
        write(".gitignore", ("\n".join(rules["ignore"]) + "\n").encode())
        for v, source in sorted(roms.items()):
            path = f"roms/baserom.{v}.z64"
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, root / path)
            created.append(path)
        for v in sorted(roms):
            rom, split = f"roms/baserom.{v}.z64", f"versions/{v}/{name}.yaml"
            symbol_file = f"versions/{v}/symbol_addrs.txt"
            before = set(root.glob("*.yaml"))
            result = process.run("splat create_config", [str(host.tools["splat"]), "create_config", rom], root,
                                 tmp=process.scratch(root))
            _checked(result, "setup.splat", "splat create_config")
            made = set(root.glob("*.yaml")) - before
            if len(made) != 1:
                raise Refusal(Finding("setup.splat", f"expected one new yaml, found {len(made)}", versions=(v,)))
            (root / split).parent.mkdir(parents=True, exist_ok=True)
            made.pop().rename(root / split)
            document = yaml.safe_load((root / split).read_bytes())
            document["options"].update({k: value.format(version=v, name=name, rom=rom)
                                        for k, value in rules["splat"]["options"].items()})
            write(split, yaml.safe_dump(document, sort_keys=False).encode())
            write(symbol_file, b"")
            blocks.append(f'\n[version.{v}]\nbaserom = {json.dumps(rom)}\nbaserom_sha1 = "{versions[v]["sha1"]}"\n'
                          f'split = {json.dumps(split)}\nsymbols = {json.dumps(symbol_file)}\nmacros = []\n')
        values = {key: json.dumps(params[key]) for key in ("name", "title", "names_from", "toolchain")}
        write("config.toml", _render("config.toml", **values, id=json.dumps(str(uuid.uuid4())),
                                     versions=json.dumps(sorted(roms)), version_blocks="".join(blocks)))
        write("layout.toml", b"schema = 3\ncap = 32\ngroup = []\nunit = []\n")
        write(symbols.path(), symbols.empty())
        write("README.md", _render("README.md.in", title=params["title"],
                                   begin=rules["readme"]["begin"], end=rules["readme"]["end"]))
        for path, value in _extras(rules, params["title"], sorted(roms)).items():
            write(path, value)
        for args in (["init"], ["add", "-A"], ["-c", f"user.name={host.author[0]}", "-c",
                     f"user.email={host.author[1]}", "commit", "-m", f"init {name}"]):
            _checked(process.run("git init repository", [str(host.tools["git"]), *args], root,
                                 tmp=process.scratch(root)),
                     "native.exit", "git " + " ".join(args))
        return {"dir": str(root), "created": sorted(created), "versions": versions}
def files(snapshot: Snapshot) -> dict[str, bytes]:
    with effort.stage("repo.files"):
        rules, project = configuration.load_resource("repo.toml"), snapshot.config.project
        pins = {k: f"{r['action']}@{r['sha']} # {r['tag']}" for k, r in rules["github"].items()}
        uploads = "\n".join(_render("upload.yml.in", upload=pins["upload"], version=v).decode().rstrip("\n")
                            for v in project.versions)
        wanted = {"Makefile": build.makefile(snapshot),
                  **report.files(snapshot),
                  **{f"versions/{v}/symbols.ld": build.symbols_ld(snapshot, v) for v in project.versions},
                  ".gitignore": ("\n".join(rules["ignore"]) + "\n").encode(),
                  ".github/workflows/ci.yml": _render("ci.yml.in", checkout=pins["checkout"], uploads=uploads),
                  ".gitlab-ci.yml": _render("gitlab-ci.yml.in", image=rules["gitlab"]["image"],
                                            packages=" ".join(rules["gitlab"]["packages"]))}
        # the manifest lists every generated file; the starter files below are the author's once they exist
        wanted[_MANIFEST] = "".join(f"{p}\n" for p in sorted({*wanted, _MANIFEST})).encode()
        for path, value in _extras(rules, project.title, project.versions).items():
            if snapshot.peek(path) is None:
                wanted[path] = value
        return wanted
def _extract_job(job):
    return build.extract(*job)
def _prove(snapshot: Snapshot, versions) -> dict:
    owned = {m for unit in snapshot.layout.units.values() for m in unit.members}
    jobs = [(snapshot, layout.asm_unit(snapshot, m.name, v), v) for m in snapshot.layout.members.values()
            if m.kind == "function" and m.name not in owned for v in m.holders() if v in versions]
    counts, missing, bad = {v: {"members": 0, "exact": 0, "bytes": 0} for v in versions}, [], []
    for (_, unit, v), proofs in zip(jobs, pool.map(snapshot.config, "repo.prove", native.prove_job, jobs,
                                                   lambda job: native.stamp(*job)), strict=True):
        member = unit.members[0]
        exact = bool(proofs) and all(p.exact for p in proofs)
        counts[v]["members"] += 1
        counts[v]["exact"] += int(exact)
        counts[v]["bytes"] += sum(p.size for p in snapshot.layout.members[member].placements if p.version == v)
        if not exact:
            bad.append(v)
            missing.extend(s for p in proofs for s in p.missing)
            if not proofs:
                missing.append(f"{v}: {member}: no proof")
    if bad:
        raise Refusal(Finding("setup.not_exact", f"{len(bad)} extracted members do not rebuild their ROM bytes",
                              missing=tuple(missing[:20]), versions=tuple(sorted(set(bad)))))
    return counts
def _built(snapshot: Snapshot) -> dict[str, dict[str, int]]:
    """Per version the compiled units built and the ones withheld there."""
    phases = configuration.load_resource("units.toml")["kind"]
    out = {v: {"built": 0, "withheld": 0} for v in snapshot.config.project.versions}
    for unit in snapshot.layout.units.values():
        if "compile" in phases[unit.kind]["phases"]:
            for v in {v for n in unit.members for v in snapshot.layout.members[n].holders()}:
                out[v]["withheld" if v in unit.withheld else "built"] += 1
    return out
def _paths(project) -> dict[str, str]:
    return {v: vf.symbols for v, vf in project.version_files.items()}
def _changed(snapshot: Snapshot, wanted: Mapping[str, bytes]) -> dict[str, bytes]:
    return {path: value for path, value in wanted.items() if snapshot.peek(path) != value}
_MANIFEST = "generated.txt"
def _stale(snapshot: Snapshot, wanted: Mapping[str, bytes]) -> dict[str, None]:
    """Files the previous manifest lists that this setup no longer generates, and nothing else."""
    try:
        before = snapshot.read(_MANIFEST).decode().splitlines()
    except FileNotFoundError:
        return {}
    gone = {}
    for path in sorted(set(before) - wanted.keys()):
        if path.startswith("/") or ".." in path.split("/"):
            raise Refusal(Finding("config.file", f"{_MANIFEST} lists {path}, which is not inside the project",
                                  path=_MANIFEST))
        if snapshot.peek(path) is not None:
            gone[path] = None
    return gone
def _plan(snapshot: Snapshot, writes: Mapping[str, bytes | None], operation: str, message: str) -> Plan:
    return Plan(operation, snapshot.digest, writes, (), (), (), message, digest((snapshot.digest, writes, message)))
@cache
def _splat_version(tool: Path, root: Path) -> bytes:
    result = process.run("splat version", [str(tool), "--version"], tool.parent, tmp=process.scratch(root))
    if result.exit:
        raise Refusal(Finding("setup.splat", result.stderr.decode(errors="replace")))
    return result.stdout
def _stamp(config: Config, v: str, symbol_file: bytes | None = None) -> str:
    root, vf = config.project.root, config.project.version_files[v]
    symbol_file = (root / vf.symbols).read_bytes() if symbol_file is None else symbol_file
    return digest(((root / vf.split).read_bytes(), symbol_file,
                   configuration.load_resource("repo.toml")["splat"],
                   _splat_version(config.host.tools["splat"], root),
                   hashlib.sha1((root / vf.baserom).read_bytes()).hexdigest()))
def _stamps(config: Config) -> dict:
    """The splat stamps of the last extraction, by version."""
    path = config.project.root / ".unbake/extract.json"
    return json.loads(path.read_bytes()) if path.exists() else {}
def _state(config: Config) -> str:
    """Everything setup reads or writes: the tool, the repository, the ROMs and what was built. Unchanged means done."""
    root = config.project.root
    head = process.git(config, "rev-parse", "HEAD").stdout.decode().strip()
    tool = digest([p.read_bytes() for p in sorted(Path(__file__).parent.rglob("*"))
                   if p.is_file() and p.suffix != ".pyc"])  # the tool's content, not when it was touched
    top = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.iterdir()) if p.is_file()]
    return digest((head, config.digest, tool, top, build.inputs(config), _stamps(config)))
def _drop_stale_facts(config: Config) -> None:
    """What extraction emitted (splat's undefined symbol lists, the disassembler context) is only true for the inputs
    it ran on. A version whose stamp no longer matches the split file, the ROM or the symbol file the table will
    render has its facts removed before they are read, so a stale list never refuses a setup that replaces it."""
    root, project = config.project.root, config.project
    stamps = _stamps(config)
    table = symbols.parse((root / symbols.path()).read_bytes(), project.versions)
    for v in project.versions:
        if stamps.get(v) != _stamp(config, v, symbols.render(table, v)):
            for path in [*versions.fact_files(config, v)[0], *(root / ".unbake/symbols" / v).glob("*.csv")]:
                (root / path).unlink(missing_ok=True)
def setup(config: Config, params: Json) -> Json:
    with effort.stage("repo.setup"), store.exclusive(config, "land") as held:
        adapters.check_abucache()
        if not held:
            raise Refusal(Finding("setup.busy", configuration.sentence("setup.busy")))
        journal.recover(config)
        done, state = config.project.root / ".unbake/setup.json", _state(config)
        if done.exists() and json.loads(done.read_bytes())["state"] == state:
            effort.count("setup", True)
            return {**json.loads(done.read_bytes())["result"], "commits": [], "extracted": [], "files": []}
        effort.count("setup", False)
        _drop_stale_facts(config)
        project, commits, written = config.project, [], set()
        def apply(plan: Plan) -> None:
            nonlocal snapshot
            commits.append(journal.apply(config, plan, snapshot.commit))
            written.update(plan.writes)
            snapshot = replace(layout.overlay(snapshot, plan.writes), commit=commits[-1])
        def extract(versions) -> None:
            """Splat runs for the versions whose inputs moved since its last run, wherever setup asks."""
            stale = [v for v in versions if stamps.get(v) != _stamp(config, v)]
            for v in versions:
                effort.count("extract", v not in stale)
            results = pool.map(config, "repo.extract", _extract_job, [(config, v) for v in stale])
            for v, result in zip(stale, results, strict=True):
                _checked(result, "setup.splat", f"extract {v}")
                stamps[v] = _stamp(config, v)
            _write(project.root / ".unbake/extract.json", json.dumps(stamps, sort_keys=True).encode())
            extracted.update(stale)
        with effort.stage("repo.setup.toolchains"):
            snapshot = layout.capture(config)
            table = symbols.edit(snapshot)
            writes = _changed(snapshot, symbols.files(table, _paths(project)))
            writes.update({path: value for path, value in _extras(configuration.load_resource("repo.toml"),
                           project.title, project.versions).items() if not (project.root / path).exists()})
            if writes:
                apply(_plan(snapshot, writes, "setup", "setup: extraction inputs"))
            toolchains = sorted({project.toolchain, *(u.toolchain for u in snapshot.layout.units.values())})
            for id in toolchains:
                build.install_toolchain(config, id)
        with effort.stage("repo.setup.extract"):
            extracted, stamps = set(), _stamps(config)
            extract(project.versions)
        with effort.stage("repo.setup.prove"):
            snapshot = layout.capture(config)
            proof = _prove(snapshot, project.versions)
        joined = 0
        def converge() -> Plan:
            """One map pass: plan, read the names the versions give one thing and the addresses the ROM code forms,
            plan again on what those give, and write all of it in one commit. The versions it changed are extracted
            again. A second pass must find nothing to write; if it does, the layout is not a function of the ROMs and
            setup refuses naming what moved."""
            nonlocal snapshot, joined
            snapshot = layout.capture(config)
            mapped = infer.plan(snapshot)
            if mapped.blocking:  # landed C that does not compile in a version it holds is not debt: setup refuses
                raise Refusal(*mapped.blocking)
            table = symbols.edit(snapshot)  # names the versions give one thing become one symbol
            found = symbols.join_pairs(snapshot, table, infer.correspondences(snapshot)) + symbols.fix_rows(
                table, infer.addresses(snapshot, mapped.debt))
            writes, message = dict(mapped.writes), mapped.message
            if found:  # plan again on the table the readings give, so the same commit holds the layout it implies
                writes.update(_changed(snapshot, symbols.files(table, _paths(project))))
                settled = infer.plan(layout.overlay(snapshot, writes))
                writes.update(settled.writes)
                message = f"{settled.message}; {found} symbols read from the ROM"
            if not writes:
                return mapped
            planned = layout.overlay(snapshot, writes).digest  # the snapshot these writes give, before extraction
            apply(_plan(snapshot, writes, "layout", message))
            joined += found
            changed = [v for v, vf in project.version_files.items() if {vf.split, vf.symbols} & writes.keys()]
            if changed:
                moved.update(changed)
                extract(changed)
            snapshot = layout.capture(config)
            if snapshot.digest == planned:  # extraction gave the facts the plan assumed: the plan is its own fixpoint
                return replace(mapped, writes={})
            again = infer.plan(snapshot)  # extraction told something else: the layout must still hold under it
            if again.writes:
                raise Refusal(Finding("layout.nonconvergent", "a second map pass still changes "
                                      f"{', '.join(sorted(again.writes)[:8])} ({len(again.writes)} files) after the "
                                      f"first pass wrote {len(writes)} files: {again.message}"))
            return again
        moved: set[str] = set()  # versions extracted again since the proof: proved once, when the layout settles
        with effort.stage("repo.setup.map"):
            mapped = converge()
            if moved:
                proof.update(_prove(layout.capture(config), sorted(moved)))
                moved.clear()
            snapshot = layout.capture(config)
            writes = _changed(snapshot, {"types.toml": types.scan(snapshot)})
            if writes:
                apply(_plan(snapshot, writes, "layout", "setup: type map"))
        with effort.stage("repo.setup.boundary"):
            plan, boundary = layout.boundary_plan(snapshot)
            if plan.writes:
                apply(plan)
                changed = [v for v, vf in project.version_files.items() if vf.split in plan.writes]
                extract(changed)
                snapshot = layout.capture(config)
                proof.update(_prove(snapshot, changed))
                if layout.boundary_plan(snapshot)[0].writes:
                    raise Refusal(Finding("layout.nonconvergent", configuration.sentence("layout.nonconvergent")))
                mapped = converge()  # the functions moved: the units and their data follow
                if moved:
                    proof.update(_prove(layout.capture(config), sorted(moved)))
        with effort.stage("repo.setup.files"):
            wanted = files(snapshot)
            writes = {**_changed(snapshot, wanted), **_stale(snapshot, wanted)}
            if writes:
                apply(_plan(snapshot, writes, "setup", "setup: repository files"))
            local = project.root / "local.mk"
            if _write(local, build.local_mk(config)):
                written.add("local.mk")
        with effort.stage("repo.setup.make"):
            make = build.make_check(config)
        type_map, conflicts = types.load(snapshot), types.conflicts(snapshot)
        result = {"toolchains": toolchains, "extracted": sorted(extracted), "proof": proof,
                "groups": len(snapshot.layout.groups),
                "subsystems": dict(Counter(g.subsystem for g in snapshot.layout.groups.values())),
                "types": {plural: len(type_map[key]) for plural, key in
                          (("functions", "function"), ("globals", "global"), ("structs", "struct"))},
                "boundary": boundary, "joined": joined, "units": _built(snapshot),
                "debt": [*(f.reason for f in mapped.debt),
                        *(f"{f.reason}: " + "; ".join(f.missing) for f in conflicts)],
                "commits": commits, "files": sorted(written), "make": make}
        _write(done, json.dumps({"state": _state(config), "result": result}).encode())
        return result
