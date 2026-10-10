# Frozen contracts, revision 4

Revision 3 is archived at `L/contracts/rev3/`; ITER2.md says what changed and why. `L/contracts/unbake/contracts.py` and `L/contracts/unbake/resources/` are final and validated (`L/contracts/validate.py`: 55 checks, 0 errors). The integrator copies them into the worktree before any lane starts; lanes never edit them. This file is shipped as `src/unbake/resources/CONTRACTS.md`; `tests/test_lint.py` reads its API and budget tables.

## Records (contracts.py)

`Json`, `digest(value) -> str`, `Origin` (+`note`), `Finding`, `Refusal(*findings)`, `Host`, `Resident`, `VersionFiles` (+`meta`), `Project` (+`title`), `Config`, `Placement`, `Member`, `Version`, `Group`, `UnitSpec`, `LayoutMap` (+`fuzzy`), `Snapshot`, `Recipe`, `SourceView`, `NativeResult`, `Proof` (+`score`, `symptoms`), `Plan`, `Receipt`, `Submission` (new), `Attempt` (new), `StageRecord`; protocols `Toolchain`, `Assembler`, `Linker`, `Span`. All records are frozen dataclasses and picklable. Every record written to disk is `json.dumps(asdict(record), sort_keys=True)` and is validated after `json.loads` (tuples become arrays) against the schema of the same name.

## Invariants

1. A `Refusal` always carries at least one `Finding`; every `Finding.key` is a key of `resources/data/keys.toml`.
2. `Finding.blocking=False` marks project debt. Debt never raises; it is returned and counted.
3. A `Proof` with `exact=False` has nonempty `missing` and `score < 1.0`; `exact=True` has `score == 1.0` and `symptoms == {}`. `land.not_exact` findings copy the `missing` strings and merge the proofs' symptoms.
4. Config values have no defaults. A missing or unknown key refuses with `config.schema` naming `file:dotted.path`. `"auto"` is an explicit host value for `resources.cores`/`resources.workers`; config resolves it to `len(os.sched_getaffinity(0))` and records `Origin.note = "auto: os.sched_getaffinity -> N"`.
Host tools used only by particular phases/commands are optional (`cpp`, `mips_as`, `mips_ld`, `mips_objcopy`, `mips_objdump`, `n64link`, `armips`). Config validates every supplied path as executable. A phase needing an omitted tool refuses with `native.missing_tool`, naming the tool and unit kind; no PATH fallback. Migration writes only supplied tools.

5. Every cache key is `contracts.digest(...)` of content (bytes digests, recipe digest, tool pins), never mtimes or reprs.
6. Only `journal.apply` writes tracked project files, and only `land.drain` and `repo.setup` call it, `repo.setup` while it holds `store.exclusive(config, "land")`, `land.drain` per claimed entry with the commit alone serialised by `journal.lock`. Every other writer writes only untracked runtime files under `<project>/.unbake/` (inbox, ledger, stages, check.json), `local.mk`, view's content-keyed `build/views/<digest>/` materialisation (kept from revision 3), or the host cache, each by atomic create/rename or `O_APPEND`.
7. Only process.py starts processes, only pool.py creates worker processes, only effort.py, process.py and pool.py read rusage. Lint greps imports of `subprocess`, `multiprocessing`, `concurrent.futures`, `threading`, `os.fork`.
8. `view.all_versions` returns exactly the requested version keys.
9. Toolchain, assembler, linker, unit-kind and repository-file behaviour comes from resources; the strings `"gcc"`, `"ido"`, `"c"`, `"asm"`, `"data"`, `"resource"` appear only in resources and tests (lint).
10. Credit is state, not history: a member is matched in a version iff it belongs to a layout unit whose kind has `decompiled = true` and holds that version. The landing gate guarantees exactness; `make check` re-verifies. The ledger is an operational log and is never read for credit.
11. Locks, all kernel `flock`, each justified: `store.exclusive("land")` (held by setup for its whole run, never waited on: a drain that finds it held returns `running: true`; the commit itself is serialised by `journal.lock`, so measuring takes no lock), `store.slots` (host-wide native concurrency equals `host.workers` across all processes; blocking acquire is the signal), no cache lock (a miss produces unlocked and stores atomically, so production may fan out to the pool). There are no per-group claims.

## Public API (every lane implements exactly its row and calls only these)

NEW rows: human, types, symptoms, land, repo. CHANGED rows: config, effort, pool, store, cli, adapters, layout, infer (data name only), native, compare, headers (internals), publish, report, build. All other rows are unchanged and their current worktree code is kept (WORKORDERS/INDEX.md).

| Module | Public functions |
|---|---|
| config | `version_files(table: Json) -> dict[str, VersionFiles]`; `load_resource(name: str) -> Json`; `toml(schema: str, data: bytes, file: str, key: str = 'config.schema') -> Json`; `validate(schema: str, value: Json, file: str) -> None`; `load_host(path: Path) -> Host`; `load_project(root: Path) -> Project`; `load(root: Path, host: Path) -> Config`; `sentence(key: str) -> str`; `template(name: str) -> str` |
| effort | `command(name: str, argv: Sequence[str]) -> ContextManager[None]`; `bind(config: Config) -> None`; `stage(name: str) -> ContextManager[Span]`; `record_native(name: str, result: NativeResult, start_ns: int) -> None`; `record_pool(name: str, items: int, jobs: int, admitted: int, start_ns: int, envelopes: Sequence[Json]) -> None`; `progress(name: str, done: int, total: int) -> None`; `waited(seconds: float) -> None`; `listen(callback: Callable[[str, Json], None]) -> None`; `count(kind: str, hit: bool) -> None`; `capture() -> ContextManager[list[StageRecord]]`; `counters() -> dict[str, tuple[int, int]]`; `closed() -> tuple[StageRecord, ...]`; `tree() -> Json`; `invocation() -> str`; `forget(*kinds: str) -> None`; `memo(key: object, produce: Callable[[], object]) -> object` |
| human | `attach(stream: TextIO, tty: bool) -> None`; `finding(finding: Finding) -> str`; `summary(records: Sequence[StageRecord]) -> list[str]`; `finish(command: str, result: Json \| None, findings: Sequence[Finding]) -> None` |
| process | `run(name: str, argv: Sequence[str], cwd: Path, *, tmp: Path, stdin: bytes = b"", stdout_path: Path \| None = None, timeout: float \| None = None, outputs: Mapping[str, Path] = {}) -> NativeResult`; `scratch(root: Path) -> Path`; `tool(config: Config, name: str, *, kind: str | None = None) -> Path`; `git(config: Config, *args: str) -> NativeResult` |
| pool | `map(config: Config, name: str, function: Callable[[Any], Any], items: Sequence[Any], key: Callable[[Any], str | None] | None = None) -> list[Any]`; `gather(config: Config, groups: Sequence[tuple]) -> list[list[Any]]` |
| store | `write(path: Path, content: bytes) -> bool`; `content(config: Config) -> ContentCache`; `cached(config: Config, kind: str, key: str, produce: Callable[[], bytes]) -> bytes`; `get(config: Config, kind: str, key: str) -> bytes | None`; `put(config: Config, kind: str, key: str, value: bytes) -> None`; `put_many(config: Config, kind: str, rows: Iterable[tuple[str, bytes]]) -> None`; `stem(member: str) -> str`; `listing(config: Config, folder: str) -> frozenset[str]`; `log(config: Config, kind: str, body: Json) -> None`; `append(config: Config, stream: str, row: Json) -> None`; `rows(config: Config, stream: str) -> list[Json]`; `exclusive(config: Config, name: str, *, wait: bool) -> ContextManager[bool]`; `slots(config: Config, want: int) -> ContextManager[int]`; `work(config: Config) -> ContextManager[Path]` |
| journal | `apply(config: Config, plan: Plan, head: str) -> str`; `recover(config: Config) -> bool` |
| cli | `main(argv: Sequence[str] \| None = None) -> int` |
| adapters | `toolchain(config: Config, id: str) -> Toolchain`; `assembler(config: Config, kind: str) -> Assembler`; `linker(config: Config) -> Linker`; `script(config: Config, recipe: Recipe, version: str) -> str`; `render(template: Sequence[str], values: Mapping[str, Sequence[str]]) -> list[str]`; `assemble_template(row: Json \| None) -> tuple[tuple[str, ...], str]`; `host_tools(config: Config) -> dict[str, str]`; `link_script(sections: Sequence[Placement], symbols: Path) -> str`; `chain_steps(config: Config, id: str, recipe: Recipe, include: Sequence[Path], stem: str) -> tuple[Step, ...]`; `tool_identity(config: Config, id: str) -> str`; `check_abucache() -> None`; `link_commands(tools: Mapping[str, str], obj: Path, sections: Sequence[Placement], rom: Path, windows: Sequence[Resident], symbols: Path, work: Path, trim: bool, *, score: bool = False) -> tuple[tuple[str, ...], ...]` |
| recipes | `resolve(config: Config, unit: UnitSpec, overrides: Json) -> Recipe` |
| versions | `fact_files(config: Config, vid: str) -> tuple[list[str], list[str]]`; `read(config: Config, reader: Callable[[str], bytes], only: Collection[str] | None = None) -> dict[str, Version]`; `rows(version: Version, reader: Callable[[str], bytes]) -> list[tuple[str, str, Placement]]`; `section_of(version: Version, kind: str, where: str) -> str`; `rom_bytes(version: Version, start: int, end: int) -> bytes`; `delay_slot(word: int) -> bool`; `rom_view(version: Version) -> mmap.mmap`; `asm_path(config: Config, version: str, member: str) -> Path`; `undefined(obj: Path) -> tuple[str, ...]`; `resolve(version: Version, names: Sequence[str], unit: str) -> tuple[Finding, ...]`; `facts_digest(version: Version) -> str`; `unowned(name: str) -> bool` |
| view | `get(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, *, lines: bool = True) -> SourceView`; `all_versions(snapshot: Snapshot, unit: UnitSpec, recipe: Recipe, versions: Sequence[str]) -> dict[str, SourceView]`; `active_lines(view: SourceView, path: str) -> dict[int, str]`; `closure(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[tuple[str, str], ...] | None`; `headers(snapshot: Snapshot, unit: UnitSpec, argv: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]` |
| layout | `capture(config: Config) -> Snapshot`; `overlay(snapshot: Snapshot, writes: Mapping[str, bytes \| None]) -> Snapshot`; `load_map(config: Config, versions: Mapping[str, Version], text: bytes, reader: Callable[[str], bytes]) -> LayoutMap`; `dump_map(layout: LayoutMap) -> bytes`; `claim_rows(snapshot: Snapshot, claims: Sequence[Claim]) -> dict[str, bytes]`; `unit_of(snapshot: Snapshot, member: str) -> UnitSpec \| None`; `asm_unit(snapshot: Snapshot, member: str, version: str) -> UnitSpec`; `unit_options(snapshot: Snapshot, member: str, source: bytes) -> list[tuple[UnitSpec, dict[str, bytes \| None]]]`; `boundary_plan(snapshot: Snapshot) -> tuple[Plan, Json]` |
| infer | `groups(snapshot: Snapshot) -> tuple[Group, ...]`; `sdk(snapshot: Snapshot) -> frozenset[str]`; `pairs(snapshot: Snapshot, directions: Sequence[tuple[str, str]]) -> dict[tuple[str, str], dict[str, str]]`; `correspondences(snapshot: Snapshot) -> tuple[tuple[str, str, str, str], ...]`; `addresses(snapshot: Snapshot, debt: Sequence[Finding]) -> dict[tuple[str, str], tuple[int, bool]]`; `plan(snapshot: Snapshot) -> Plan` |
| symbols | `path() -> str`; `empty() -> bytes`; `parse(data: bytes, versions: Collection[str]) -> dict[str, dict]`; `load(reader: Callable[[str], bytes], versions: Collection[str]) -> dict[str, dict]`; `edit(snapshot: Snapshot) -> dict[str, dict]`; `files(table: Mapping[str, Mapping], paths: Mapping[str, str]) -> dict[str, bytes]`; `dump(table: Mapping[str, Mapping]) -> bytes`; `fix_rows(table: dict[str, dict], found: Mapping[tuple[str, str], tuple[int, bool]]) -> int`; `declared(table: Mapping[str, Mapping], version: str) -> dict[str, int]`; `render(table: Mapping[str, Mapping], version: str) -> bytes`; `rename(table: dict[str, dict], old: str, new: str) -> None`; `rewrite(text: str, old: str, new: str) -> str`; `tokens(text: str) -> set[str]`; `referenced(snapshot: Snapshot, table: Mapping[str, Mapping]) -> set[str]`; `join(table: dict[str, dict], a: str, b: str, used: Collection[str], source: str) -> str | None`; `join_pairs(snapshot: Snapshot, table: dict[str, dict], pairs: Collection[tuple[str, str, str, str]]) -> int` |
| evidence | `read(blob: bytes, rom: bytes, vram: int) -> dict[str, tuple[set[int], bool]]` |
| rename | `run(config: Config, params: Json) -> Json` |
| ownership | `emitted(blob: bytes) -> dict[str, tuple[bytes, dict[int, bool], list[tuple[int, int]]]]`; `locate(index: tuple, blob: bytes, text: tuple[int, int] | None) -> dict[str, list | dict]`; `claims(snapshot: Snapshot, version: str \| None = None, units: Mapping[str, UnitSpec] \| None = None) -> tuple[dict[str, UnitSpec], tuple[Claim, ...], list[Finding]]`; `derive_many(snapshot: Snapshot, units: Sequence[UnitSpec]) -> list[tuple[Snapshot, UnitSpec]]`; `derive(snapshot: Snapshot, unit: UnitSpec) -> tuple[Snapshot, UnitSpec]`; `exact(snapshot: Snapshot, units: dict[str, UnitSpec]) -> tuple[dict[str, UnitSpec], list[Finding]]` |
| types | `scan(snapshot: Snapshot) -> bytes`; `conflicts(snapshot: Snapshot) -> list[Finding]`; `load(snapshot: Snapshot) -> Json`; `landed(snapshot: Snapshot, unit: UnitSpec, view: SourceView) -> bytes` |
| native | `sections(snapshot: Snapshot, unit: UnitSpec, version: str) -> tuple[Placement, ...]`; `objects(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, work: Path) -> tuple[Path, tuple[NativeResult, ...]]`; `measure(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, obj: Path, work: Path) -> tuple[Proof, ...]`; `prove(snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, work: Path) -> tuple[Proof, ...]`; `stamp(snapshot: Snapshot, unit: UnitSpec, version: str) -> str | None`; `prove_job(item: tuple[Snapshot, UnitSpec, str]) -> tuple[Proof, ...]`; `prove_args(item: tuple) -> tuple[Proof, ...]` |
| symptoms | `measure(built: bytes, target: bytes, section: str) -> Json`; `score(built: bytes, target: bytes) -> float`; `from_missing(missing: Sequence[str]) -> Json`; `merge(facts: Sequence[Json]) -> Json`; `diff(built: bytes, target: bytes, vram: int) -> str` |
| compare | `holders(snapshot: Snapshot, unit: UnitSpec) -> tuple[str, ...]`; `measure(snapshot: Snapshot, unit: UnitSpec, overrides: Json, out: Path \| None) -> tuple[Proof, ...]`; `measure_many(snapshot: Snapshot, unit: UnitSpec, variants: Sequence[Json], out: Path \| None = None) -> list[tuple[Proof, ...]]`; `gaps(snapshot: Snapshot, unit: UnitSpec, proofs: Sequence[Proof]) -> tuple[Finding, ...]`; `bind(snapshot: Snapshot, file: Path, function: str \| None) -> tuple[UnitSpec, Snapshot]`; `run(config: Config, params: Json) -> Json` |
| headers | `normal(text: str) -> str`; `sources(snapshot: Snapshot, place: str = 'include', suffix: str = '.h') -> list[str]`; `disagreements(snapshot: Snapshot, defined: Collection[str] = ()) -> tuple[dict[str, str], list[Finding]]`; `catalog(snapshot: Snapshot, version: str) -> dict[str, tuple[str, int, str]]`; `fold(snapshot: Snapshot, unit: UnitSpec, view: SourceView) -> tuple[dict[str, bytes \| None], tuple[Finding, ...]]` |
| policy | `evaluate(snapshot: Snapshot, path: str, text: str, view: SourceView \| None, sdk_group: bool) -> tuple[Finding, ...]`; `scope(before: Sequence[Finding], after: Sequence[Finding], writes: Collection[str]) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]`; `census(snapshot: Snapshot) -> tuple[Finding, ...]` |
| publish | `admit(snapshot: Snapshot, request: Json) -> tuple[UnitSpec, Snapshot, tuple[Proof, ...]]`; `plans(snapshot: Snapshot, unit: UnitSpec, proposed: Snapshot) -> list[Plan]`; `land(config: Config, submission: Submission) -> Receipt` |
| land | `submit(config: Config, request: Json, unit: UnitSpec, proofs: Sequence[Proof], source: bytes, origin: str) -> Submission \| None`; `inbox(config: Config) -> list[Submission]`; `drain(config: Config) -> Json`; `submit_command(config: Config, params: Json) -> Json`; `land_command(config: Config, params: Json) -> Json`; `check_command(config: Config, params: Json) -> Json` |
| report | `current(snapshot: Snapshot) -> Json`; `items(snapshot: Snapshot, params: Json) -> list[Json]`; `objdiff(snapshot: Snapshot, report: Json, version: str) -> Json`; `readme(text: str, snapshot: Snapshot, report: Json) -> str`; `split_slot(snapshot: Snapshot, member: str) -> str | None`; `files(snapshot: Snapshot) -> dict[str, bytes]`; `run(config: Config, params: Json) -> Json` |
| build | `makefile(snapshot: Snapshot) -> bytes`; `local_mk(config: Config) -> bytes`; `symbols_ld(snapshot: Snapshot, version: str) -> bytes`; `inputs(config: Config) -> str`; `extract(config: Config, version: str) -> NativeResult`; `install_toolchain(config: Config, id: str) -> Path`; `make_check(config: Config) -> Json` |
| repo | `validate_roms(roms: Mapping[str, Path], names_from: str) -> Json`; `init(params: Json) -> Json`; `files(snapshot: Snapshot) -> dict[str, bytes]`; `setup(config: Config, params: Json) -> Json` |

Removed public functions: `store.claim`, `store.ledger_append`, `store.ledger`, `publish.publish`, `publish.repair`, `queue.*`, `search.run`, `report.next_action`, `build.init`, `build.setup`.

## Shapes shared between modules

- `request` (admission) = `{"file": str, "function": str | None, "overrides": {"toolchain"?: str, "add": [str], "omit": [str]}, "note": str}`.
- CLI `params` = the click parameter dict with the names declared in `commands.toml` (`-` becomes `_`).
- Every CLI result validates against `schemas/result.<command>.schema.json` (`report --next` against `result.next`); the stdout line validates against `envelope`.
- DRAIN (`result.land`, embedded in submit/compare/check results) = `{running, landed: [{id, member, operation, commit}], refused: [{id, member, findings}]}`.
- Built bytes: `native.measure` stores each proof's built material (sections in placement order) with `store.put(config, "bytes", proof.built_sha256, material)`; readers fetch it with `store.cached(config, "bytes", sha, produce)` where `produce` raises `Refusal(Finding("store.corrupt", ...))`.
- effort events passed to `listen` callbacks: `("open", {"path": [str]})`, `("close", {"path": [str], "wall": float, "status": str})`, `("progress", {"name": str, "done": int, "total": int})`.

## Runtime paths (project)

| Path | Writer | Format |
|---|---|---|
| `.unbake/inbox/<id>.json`, `<id>.c` | land.submit (create-new, then rename `.part`) | submission schema; source copy |
| `.unbake/inbox/done/<id>.json`, `.unbake/inbox/refused/<id>.json` | land.drain (rename) | same file moved |
| `.unbake/ledger.jsonl` | store.log | ledger schema |
| `.unbake/stages.jsonl` | effort | stage schema |
| `.unbake/check.json` | land.check_command | `{commit, counts, findings}` |
| `.unbake/extract.json` | repo.setup | `{version: digest(split yaml bytes, rom sha256)}` |
| `local.mk` | repo.setup | rendered `local.mk.in` |

Symbols: `symbols.toml` is the one project table. A row `[symbol.NAME]` holds `kind` (function or data) and a vram per version that has the symbol. A name is one identity and a label only: no code reads a version, an address or a meaning out of it. `versions/<v>/symbol_addrs.txt` (splat input) and `versions/<v>/symbols.ld` are generated from the table (symbols.render, build.symbols_ld) and never edited by hand. Correspondences between versions are rows of the table: setup joins identities (symbols.join_pairs) and gives an identity the address another version's code shows for it. The name landed C already uses wins a join. A name that is not in the table nor among the version's generated facts is refused as `symbols.unknown`, naming the name and the unit. `unbake rename OLD NEW` refuses a name that exists or is not a C identifier, then rewrites the table row and every token-exact use in src/, include/, types.toml, regenerates the symbol files and commits it all as one journal commit.

Tracked generated files (repo.files): `Makefile`, `versions/<v>/symbols.ld`, `versions/<v>/report.json`, README.md block between `repo.toml readme.begin/end`, `.gitignore`, `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `CONTRIBUTING.md` (only when absent), SDK headers from `repo.toml [[sdk]]` (only when absent), `types.toml` (types.scan in setup; types.landed in landings).

## Templates (exact placeholder sets; validate.py checks them)

| Template | Placeholders | Renderer |
|---|---|---|
| Makefile.in | title name versions tool_defaults tree_checks roms rom_checks toolchain_rules units rom_rules | build.makefile |
| unit.mk.in | unit version outputs dependencies commands rom patches | build.makefile |
| toolchain.mk.in | stamp dir url archive sha256 files pins stamp_name | build.makefile |
| local.mk.in | assignments | build.local_mk |
| host.toml | every host value | humans |
| config.toml | id name title versions names_from toolchain version_blocks | repo.init |
| ci.yml.in, upload.yml.in | checkout uploads / upload version | repo.files |
| gitlab-ci.yml.in | image packages | repo.files |
| CONTRIBUTING.md.in | title roms | repo.files (absent only) |
| README.md.in | title begin end | repo.init |
| sdk/*.h | none (copied verbatim) | repo.files (absent only) |

## Source budgets (physical lines; planning allocations per owner 2026-10-08 — lint enforces only the total: a non-blocking warning above the soft cap of 6,000 and a failure above the hard cap of 7,500 (owner 2026-10-08))

- contracts: 330
- config: 190
- effort: 225
- human: 120
- process: 140
- pool: 150
- store: 195
- journal: 122
- cli: 120
- adapters: 230
- recipes: 88
- versions: 230
- symbols: 120
- evidence: 55
- rename: 40
- deathwatch: 6
- view: 137
- layout: 380
- infer: 420
- ownership: 330
- types: 150
- native: 210
- symptoms: 120
- compare: 150
- headers: 245
- policy: 194
- publish: 250
- land: 180
- report: 210
- build: 440
- repo: 400

Total allocated: 4,929.
