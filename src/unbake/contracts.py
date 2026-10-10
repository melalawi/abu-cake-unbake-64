"""Frozen records and protocols for unbake (contracts revision 4). Every module imports from here."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Protocol

Json = Mapping[str, Any]  # a document already validated by config.load_resource against its schema
def digest(value: Any) -> str:
    """sha256 hex of canonical JSON (sorted keys, no spaces); dataclasses, paths, bytes and sets are normalised."""
    def norm(item: Any) -> Any:  # JSON writes the rest natively
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, Mapping):
            return {str(k): v for k, v in item.items()}
        if isinstance(item, (frozenset, set)):
            return sorted(item, key=lambda v: json.dumps(v, sort_keys=True, default=norm))
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, bytes):
            return hashlib.sha256(item).hexdigest()
        raise TypeError(f"cannot digest {type(item).__name__}")
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=norm)
    return hashlib.sha256(text.encode()).hexdigest()
# ---------------------------------------------------------------- findings and refusals
@dataclass(frozen=True)
class Origin:
    key: str  # dotted config key, e.g. "resources.memory_worker_bytes"
    file: str  # absolute path of the file that set it
    sha256: str  # that file's content digest when it was read
    note: str = ""  # how a value was resolved, e.g. "auto: os.sched_getaffinity -> 12"
@dataclass(frozen=True)
class Finding:
    key: str  # a key listed in resources/data/keys.toml
    reason: str  # one plain sentence
    path: str = ""  # project-relative file
    line: int = 0  # 1-based; 0 when not line-bound
    unit: str = ""  # unit path or member name
    versions: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()  # named missing pieces; nonempty for every *.not_exact key
    origin: Origin | None = None  # config value that caused it
    blocking: bool = True  # False = project debt, reported but never refuses the operation
    action: str = ""  # exact next command or edit
    symptoms: Json = field(default_factory=dict)  # measured compare facts; absent facts never match hints
class Refusal(Exception):
    def __init__(self, *findings: Finding) -> None:
        if not findings:
            raise TypeError("Refusal needs at least one Finding")
        self.findings = tuple(findings)
        super().__init__("; ".join(f"{f.key}: {f.reason}" for f in findings))
    def __reduce__(self) -> tuple[type[Refusal], tuple[Finding, ...]]:
        return (Refusal, self.findings)  # pool workers return Refusals across processes
# ---------------------------------------------------------------- configuration
@dataclass(frozen=True)
class Host:
    cores: int  # host.toml integer, or "auto" resolved by config to len(os.sched_getaffinity(0)) (Origin.note says so)
    workers: int  # same rule; pool width, flock host slots and make -j all use this one value
    memory_parent_bytes: int
    memory_worker_bytes: int
    cache_max_bytes: int
    toolchain_root: Path
    # tools, exactly: git make cpp mips_as mips_ld mips_objcopy mips_objdump n64link splat m2c permuter armips
    tools: Mapping[str, Path]
    sdk_catalog: Path | None  # n64sym-style signature JSON, None = no SDK identification
    serial_seconds: float  # budgets.*: a leaf stage this long must use at least serial_cores
    serial_cores: float
    pool_fill: float  # a pool of pool_fanout jobs per worker must keep this share of its workers busy
    pool_fanout: float
    author: tuple[str, str]  # (publish.author_name, publish.author_email) for journal commits
    origins: Mapping[str, Origin]
    digest: str
@dataclass(frozen=True)
class Resident:
    start: int
    end: int
    address: int
    table_entry_bias: int
@dataclass(frozen=True)
class VersionFiles:
    baserom: str
    baserom_sha1: str
    split: str
    symbols: str
    meta: Json  # optional [version.<id>] cartridge_id/region/description as given; {} when none (README rows)
@dataclass(frozen=True)
class Project:
    root: Path
    id: str
    name: str
    title: str
    versions: tuple[str, ...]
    names_from: str
    toolchain: str  # default toolchain id from resources/data/toolchains.toml
    build: Json  # [build]: cppflags, cflags, asflags, gnu_asflags (lists of str)
    version_files: Mapping[str, VersionFiles]  # config owns all per-version paths and ROM sha1
    version_macros: Mapping[str, tuple[str, ...]]  # version id -> -D tokens
    resident: Mapping[str, tuple[Resident, ...]]
    layout_cap: int
    origins: Mapping[str, Origin]
    digest: str
@dataclass(frozen=True)
class Config:
    project: Project
    host: Host
    digest: str
# ---------------------------------------------------------------- versions and layout
@dataclass(frozen=True)
class Placement:
    version: str
    section: str  # ".text" | ".rodata" | ".data" | ".bss"
    rom_start: int  # bss: rom_start == rom_end
    rom_end: int
    vram: int
    bss_size: int = 0  # memory extent; no ROM bytes for .bss
    @property
    def size(self) -> int:
        return self.bss_size if self.section == ".bss" else self.rom_end - self.rom_start
@dataclass(frozen=True)
class Member:
    name: str
    kind: str  # "function" | "data"
    state: str  # splat row type: "asm" | "c" | "hasm" | "data" | "rodata" | "bin" | "resource"
    group: str
    placements: tuple[Placement, ...]  # sorted by (version, section)
    def holders(self) -> tuple[str, ...]:
        return tuple(sorted({p.version for p in self.placements}))
    def reference(self, preferred: str) -> str:
        return preferred if preferred in self.holders() else self.holders()[0]
@dataclass(frozen=True)
class Version:
    id: str
    rom: Path
    rom_sha256: str
    split: str  # project-relative splat yaml, e.g. "versions/de/Game.yaml"
    symbols_file: str  # project-relative symbol_addrs.txt
    symbols: Mapping[str, int]  # name -> vram, from versions/<id>/symbol_addrs.txt
    segments: tuple[tuple[str, int, int, int], ...]  # (name, rom_start, rom_end, vram)
    code: frozenset[str] = frozenset()  # symbol names the generated files show to be code: calls, function labels
@dataclass(frozen=True)
class Group:
    name: str
    segment: str
    members: tuple[str, ...]  # address order of names_from version, then others
    evidence: str  # "authored" | "proven" | "inferred"
    signals: tuple[str, ...]
    sdk: bool  # every member identified by the SDK signature catalog
@dataclass(frozen=True)
class UnitSpec:
    path: str  # project-relative source file, e.g. "src/code_80200610.c"
    kind: str  # id from resources/data/units.toml: "c" | "asm" | "data" | "resource"
    group: str
    members: tuple[str, ...]  # contiguous run in ROM, address order
    toolchain: str
    options: Json  # validated per-unit option overrides
    withheld: tuple[str, ...] = ()  # versions the unit is not built in: its compiled data has no exact home in the ROM
@dataclass(frozen=True)
class Claim:
    """The ROM bytes [start, end) of one section that a unit's object occupies in one version, and the row they are."""
    unit: str
    version: str
    section: str
    start: int
    end: int
    rows: tuple[str, ...] = ()
@dataclass(frozen=True)
class LayoutMap:
    cap: int
    groups: Mapping[str, Group]
    members: Mapping[str, Member]
    units: Mapping[str, UnitSpec]  # by path
    digest: str
    authored: tuple[Json, ...]  # [[authored]] rows (layout.schema.json), preserved verbatim by dump_map
    fuzzy: Mapping[str, Json]  # member -> {"path": "src/fuzzy/<member>.c", "scores": {version: 0..1}}; not built
@dataclass(frozen=True)
class Snapshot:
    """Immutable view of one project state; overlays are proposed writes not yet installed."""
    config: Config
    commit: str  # git HEAD sha at capture
    layout: LayoutMap
    versions: Mapping[str, Version]
    overlays: Mapping[str, bytes | None]  # project-relative path -> proposed bytes (None = delete)
    digest: str
    def read(self, path: str) -> bytes:
        if path in self.overlays:
            data = self.overlays[path]
            if data is None:
                raise FileNotFoundError(path)
            return data
        return (self.config.project.root / path).read_bytes()
    def peek(self, path: str) -> bytes | None:
        """read, or None when the file does not exist."""
        try:
            return self.read(path)
        except FileNotFoundError:
            return None
# ---------------------------------------------------------------- native work
@dataclass(frozen=True)
class Recipe:
    toolchain: str
    cppflags: tuple[str, ...]
    cflags: tuple[str, ...]
    asflags: tuple[str, ...]
    digest: str
@dataclass(frozen=True)
class SourceView:
    unit: str
    version: str
    key: str  # digest of (source bytes, dependency pins, recipe digest, version macros)
    text: str  # preprocessed text with line markers
    dependencies: tuple[tuple[str, str], ...]  # (project-relative path, sha256), source first
    lines: tuple[tuple[int, str, int], ...]  # (output line, file, source line) from line markers
@dataclass(frozen=True)
class NativeResult:
    argv: tuple[str, ...]
    cwd: str
    exit: int | None
    signal: int | None
    stdout: bytes
    stderr: bytes
    wall_seconds: float
    cpu_seconds: float  # wait4 user+sys of this child and its reaped descendants
    max_rss_bytes: int
    outputs: Mapping[str, Path]  # role -> file the tool wrote ("object", "asm", "linked", ...)
@dataclass(frozen=True)
class Proof:
    unit: str
    member: str
    version: str
    recipe: str  # Recipe.digest
    source_sha256: str
    object_sha256: str
    built_sha256: str  # bytes placed at the member's placements
    target_sha256: str  # ROM bytes at the same placements
    exact: bool
    missing: tuple[str, ...]  # nonempty iff not exact
    score: float  # matched fraction of the member's owned bytes in this version, 0..1; 1.0 iff exact
    symptoms: Json  # symptoms.measure vocabulary (resources/schemas/hint.schema.json match keys); {} when exact
@dataclass(frozen=True)
class Plan:
    operation: str  # "publish" | "fuzzy" | "layout" | "setup"
    base: str  # Snapshot.digest it was made from
    writes: Mapping[str, bytes | None]
    affected: tuple[str, ...]  # unit paths whose objects must be reproved
    blocking: tuple[Finding, ...]
    debt: tuple[Finding, ...]
    message: str  # git commit subject
    digest: str
@dataclass(frozen=True)
class Receipt:
    operation: str
    commit: str
    plan: str  # Plan.digest
    proofs: tuple[Proof, ...]
    debt: int
    invocation: str
# ---------------------------------------------------------------- flow: inbox and cracking feedback
@dataclass(frozen=True)
class Submission:
    """One inbox entry (.unbake/inbox/<id>.json + <id>.c). Crackers write it; only land.drain consumes it."""
    id: str  # digest((member, source_sha256, overrides, operation))
    operation: str  # "publish" | "fuzzy"
    member: str
    function: str | None  # --function given at submit time
    source: str  # project-relative inbox copy, ".unbake/inbox/<id>.c"
    source_sha256: str
    overrides: Json  # {"toolchain"?: str, "add": [str], "omit": [str]}
    base: str  # git HEAD the proofs were measured at
    proofs: tuple[Proof, ...]
    origin: str  # command that submitted: "submit" | "compare" | "crack" | "check"
    note: str  # cracker's technique note ("" when none); becomes a project hint row when it lands exact
    invocation: str
@dataclass(frozen=True)
class Attempt:
    """One measured cracking attempt (.unbake/attempts/<member>.jsonl)."""
    member: str
    step: str  # ladder step id from resources/data/flow.toml
    source_sha256: str
    recipe: str
    score: float  # minimum Proof.score over holders
    best_before: float
    outcome: str  # "exact" | "better" | "same" | "worse" | "failed"
    symptoms: Json  # symptoms of the lowest-scoring holder
    hints: tuple[str, ...]  # ids of hints whose match held
    subsystem: str
    note: str
    invocation: str
    time: str  # UTC ISO-8601
# ---------------------------------------------------------------- observability
@dataclass(frozen=True)
class StageRecord:
    invocation: str
    span: str
    parent: str | None
    path: tuple[str, ...]
    kind: str  # "command" | "stage" | "pool" | "job" | "native" | "own"
    status: str  # "ok" | "refused" | "failed" | "interrupted"
    execution: str  # "serial" | "parallel"
    start_ns: int
    wall_seconds: float
    parent_cpu_seconds: float
    worker_cpu_seconds: float
    native_cpu_seconds: float
    cores: float
    items: int
    jobs: int
    workers_used: int
    workers_admitted: int
    parent_rss_peak_bytes: int
    worker_rss_peak_bytes: int
    memory_limits: Mapping[str, Origin] = field(default_factory=dict)
    memory_limit_values: Mapping[str, int] = field(default_factory=dict)
    cache: Mapping[str, tuple[int, int]] = field(default_factory=dict)  # cache kind -> (hits, misses)
    findings: tuple[Finding, ...] = ()
    wait_seconds: float = 0.0  # time this stage itself blocked on a lock
    dispatch_seconds: float = 0.0  # pool: wall the parent spent outside waiting on workers
    busy_seconds: float = 0.0  # pool: wall the workers spent inside jobs, summed
# ---------------------------------------------------------------- adapter protocols
class Assembler(Protocol):
    def assemble(self, source: Path, recipe: Recipe, include: Sequence[Path], out: Path) -> NativeResult: ...
class Linker(Protocol):
    def link(self, obj: Path, sections: Sequence[Placement], symbols: Path, work: Path, trim: bool) -> NativeResult:
        """abucache link: ld and one objcopy per section, with only the symbols the object needs.
        outputs[section] holds source-derived bytes for each owned .text/.rodata/.data section.
        outputs[".bss"] is an ASCII size witness after the linker asserts its memory size.
        A draft is scored: ld keeps its ELF when a size assert fails, so the sizes and bytes are measured by the
        caller. A failed command, a signal or a missing output refuses link.error. Build uses the same
        link_script and link_commands. Native proofs aggregate all a member owns."""
        ...
class Span(Protocol):
    def add(self, *, items: int = 0, findings: Sequence[Finding] = ()) -> None: ...
