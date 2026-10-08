"""Optional semantic context over retained facts; never a readiness or byte-proof step."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from unbake import cache
from unbake.config import Held, Project

LABELS = (
    "engine_runtime",
    "physics_math",
    "rendering_graphics",
    "audio",
    "gameplay_design",
    "ui_menus",
    "data_tables",
)
RECIPE = "subsystems-1"


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    name: str
    version: str = ""
    path: str = ""
    storage: tuple[int, int] | None = None
    execution: tuple[int, int] | None = None
    processor: str | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    subjects: tuple[str, ...]
    origin_ref: str
    input_digest: str
    correlation_key: str
    labels: tuple[str, ...] = ()
    strength: int = 0
    semantic_anchor: bool = False
    polarity: str = "support"
    # Positive semantic support flows from the first subject to consumers only.
    propagate: bool = False
    unresolved_reason: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Family:
    id: str
    seed: str
    members: tuple[str, ...]
    basis: str


@dataclass(frozen=True)
class Facts:
    entities: tuple[Entity, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    families: tuple[Family, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    # These are existing artifact identities, not timestamps or checkout paths.
    meaning_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Membership:
    entity_id: str
    support_by_label: tuple[tuple[str, int], ...]
    state: str
    primary: str | None
    tier: str
    provisional: bool
    evidence_ids: tuple[str, ...]
    anchor_ids: tuple[str, ...]
    conflicts: tuple[str, ...]
    calibrated_probability: float | None = None


@dataclass(frozen=True)
class Snapshot:
    schema: int
    inference_recipe: str
    meaning_key: str
    evidence_key: str
    entities: tuple[Entity, ...]
    memberships: tuple[Membership, ...]
    families: tuple[Family, ...]
    boundary_edges: tuple[Evidence, ...]
    reverse_dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    missing_inputs: tuple[str, ...]

    def document(self) -> dict[str, Any]:
        return asdict(self)

    def for_subject(self, subject: str) -> tuple[Membership, ...]:
        ids = {row.id for row in self.entities if row.name == subject or row.id == subject}
        return tuple(row for row in self.memberships if row.entity_id in ids)


def canonical(facts: Facts) -> Facts:
    entities = {row.id: row for row in facts.entities}
    if len(entities) != len(facts.entities):
        raise ValueError("duplicate subsystem entity identity")
    evidence = []
    seen: set[str] = set()
    for row in sorted(facts.evidence, key=lambda row: row.id):
        if row.id in seen:
            raise ValueError("duplicate subsystem evidence identity")
        seen.add(row.id)
        if not set(row.subjects) <= entities.keys():
            raise ValueError("unbound subsystem evidence endpoint")
        if not set(row.labels) <= set(LABELS) or not 0 <= row.strength <= 8:
            raise ValueError("invalid subsystem support")
        if not row.origin_ref or not row.input_digest or not row.correlation_key:
            raise ValueError("subsystem evidence requires provenance")
        if row.polarity not in {"support", "conflict"}:
            raise ValueError("invalid subsystem evidence polarity")
        evidence.append(row)
    family_members: dict[str, set[str]] = defaultdict(set)
    family_rows = {}
    for family in sorted(facts.families, key=lambda row: row.id):
        if family.seed not in entities or not set(family.members) <= entities.keys():
            raise ValueError("unbound subsystem family")
        family_rows[family.id] = family
        family_members[family.id].update(family.members)
    families = [
        Family(key, row.seed, tuple(sorted(family_members[key])), row.basis) for key, row in sorted(family_rows.items())
    ]
    return Facts(
        tuple(entities[key] for key in sorted(entities)),
        tuple(evidence),
        tuple(families),
        tuple(sorted(set(facts.missing_inputs))),
        tuple(sorted(set(facts.meaning_refs))),
    )


def _keys(facts: Facts) -> tuple[str, str]:
    meaning = asdict(facts)
    # Provenance-only movement changes evidence context, not inferred meaning.
    for row in meaning["evidence"]:
        row.pop("origin_ref")
    meaning_key = cache.key(RECIPE, cache.serialized(meaning))
    return meaning_key, cache.key(meaning_key, cache.serialized(asdict(facts)))


def aggregate(facts: Facts) -> Snapshot:
    """Deterministic three-round directed propagation; tiers are uncalibrated priors."""
    facts = canonical(facts)
    ids = [row.id for row in facts.entities]
    direct = {key: dict.fromkeys(LABELS, 0) for key in ids}
    anchor_origins: dict[tuple[str, str], set[str]] = defaultdict(set)
    classes: dict[tuple[str, str], set[str]] = defaultdict(set)
    conflicts: dict[str, set[str]] = defaultdict(set)
    evidence_ids: dict[str, set[str]] = defaultdict(set)
    links: list[Evidence] = []
    dependencies: dict[str, set[str]] = defaultdict(set)
    counted: set[tuple[str, str, str]] = set()
    for row in facts.evidence:
        for key in row.subjects:
            evidence_ids[key].add(row.id)
            if row.polarity == "conflict":
                conflicts[key].add(row.id)
        if row.propagate and len(row.subjects) > 1:
            for target in row.subjects[1:]:
                dependencies[row.subjects[0]].add(target)
        if row.unresolved_reason or row.polarity == "conflict":
            continue
        for key in row.subjects:
            for label in row.labels:
                vote = key, label, row.correlation_key
                if vote in counted:
                    continue
                counted.add(vote)
                direct[key][label] = min(16000, direct[key][label] + row.strength * 1000)
                classes[key, label].add(row.kind)
                if row.semantic_anchor:
                    anchor_origins[key, label].add(row.correlation_key)
        if row.propagate and len(row.subjects) > 1:
            links.append(row)
    # Degree is per relationship class and distinct endpoint, never per observed access.
    neighbors: dict[tuple[str, str], set[str]] = defaultdict(set)
    unique_links: dict[tuple[str, str, tuple[str, ...]], Evidence] = {}
    for row in links:
        unique_links.setdefault((row.kind, row.correlation_key, row.subjects), row)
    links = list(unique_links.values())
    for row in links:
        source, *targets = row.subjects
        for target in targets:
            neighbors[row.kind, source].add(target)
            neighbors[row.kind, target].add(source)
    support = direct
    origins = anchor_origins
    traced_classes = classes
    for _ in range(3):
        contributions: dict[tuple[str, str, str], int] = defaultdict(int)
        next_origins = {key: set(value) for key, value in anchor_origins.items()}
        next_classes = {key: set(value) for key, value in classes.items()}
        for row in links:
            source, *targets = row.subjects
            for target in targets:
                degree = max(len(neighbors[row.kind, source]), len(neighbors[row.kind, target]))
                weight = 1000 * row.strength // (1 + degree)
                for label in LABELS:
                    value = weight * support[source][label] // (16000 * len(targets))
                    contributions[target, label, row.kind] += value
                    if value and origins.get((source, label)):
                        next_origins.setdefault((target, label), set()).update(origins[source, label])
                        next_classes.setdefault((target, label), set()).add(row.kind)
        inherited: dict[tuple[str, str], int] = defaultdict(int)
        for (target, label, _kind), value in contributions.items():
            inherited[target, label] += min(8000, value)
        support = {
            key: {label: min(16000, direct[key][label] + min(6000, inherited[key, label])) for label in LABELS}
            for key in ids
        }
        origins, traced_classes = next_origins, next_classes
    memberships = []
    for key in ids:
        ordered = sorted(LABELS, key=lambda label: (-support[key][label], label))
        tiers = {}
        for label in LABELS:
            has_anchor = bool(origins.get((key, label)))
            direct_anchor = bool(anchor_origins.get((key, label)))
            independent = len(traced_classes.get((key, label), ())) >= 2
            ticks = support[key][label]
            tiers[label] = (
                "high"
                if direct_anchor
                and ticks >= 8000
                and (
                    independent
                    or any(
                        row.kind == "verified_sdk"
                        and row.semantic_anchor
                        and row.strength == 8
                        and key in row.subjects
                        and label in row.labels
                        and not row.unresolved_reason
                        and row.polarity == "support"
                        for row in facts.evidence
                    )
                )
                else "medium"
                if has_anchor and ticks >= 5000 and (direct_anchor or independent)
                else "low"
                if ticks
                else "unknown"
            )
            if conflicts[key] and ticks:
                tiers[label] = "low"
        strongest, second = ordered[:2]
        top = support[key][strongest]
        medium = [label for label in ordered if tiers[label] in {"medium", "high"}]
        # Weak flat vectors never manufacture a mixed semantic claim.
        state = "mixed" if len(medium) > 1 else "assigned" if medium else "unknown"
        provisional = bool(top and 4 * (top - support[key][second]) < top)
        memberships.append(
            Membership(
                key,
                tuple((label, support[key][label] // 16) for label in LABELS),
                state,
                strongest if medium and not provisional else None,
                tiers[strongest],
                provisional,
                tuple(sorted(evidence_ids[key])),
                tuple(sorted(set().union(*(origins.get((key, label), set()) for label in LABELS)))),
                tuple(sorted(conflicts[key])),
            )
        )
    meaning_key, evidence_key = _keys(facts)
    return Snapshot(
        1,
        RECIPE,
        meaning_key,
        evidence_key,
        facts.entities,
        tuple(memberships),
        facts.families,
        tuple(row for row in facts.evidence if len(row.subjects) > 1),
        tuple((key, tuple(sorted(value))) for key, value in sorted(dependencies.items())),
        facts.missing_inputs,
    )


def function_id(project_id: str, version: str, start: int, end: int) -> str:
    return "function:" + cache.key(project_id, version, str(start), str(end))


def retained(project: Project, *, subjects: frozenset[str] | None = None) -> Facts:
    """Read existing inventory, header Graph, modules, and verified map; collect no new native facts."""
    from unbake import buildfiles, inputs
    from unbake.layout import map as module_map
    from unbake.layout import split
    from unbake.project.headers import Graph
    from unbake.typemap import mapping

    entities: dict[str, Entity] = {}
    evidence = []
    families = []
    missing = []
    refs = [project.id]
    functions: dict[tuple[str, str], str] = {}
    for version in project.versions:
        for row in split.functions(project, version):
            if subjects is not None and not subjects.intersection((row.name, *row.aliases)):
                continue
            key = function_id(project.id, version, row.start, row.end)
            entities[key] = Entity(
                key,
                "function",
                row.name,
                version,
                str(project.src.relative_to(project.root) / (row.path + ".c")),
                (row.start, row.end),
                (row.address, row.address + row.end - row.start),
                "host",
            )
            functions[version, row.name] = key
        for route, kind in ((buildfiles.data_bindings, "data"), (buildfiles.resource_bindings, "resource")):
            try:
                bindings = route(project, version)
            except (Held, OSError):
                missing.append(f"{version}:{kind}-bindings unavailable")
                continue
            for unit in bindings:
                source_path = (
                    unit.source
                    if isinstance(unit, buildfiles.Resource)
                    else str(project.src.relative_to(project.root) / (unit.name + ".c"))
                )
                key = kind + ":" + cache.key(project.id, version, str(unit.start), str(unit.start + unit.size))
                execution = getattr(unit, "execution_address", unit.address)
                entities[key] = Entity(
                    key,
                    kind,
                    unit.name,
                    version,
                    source_path,
                    (unit.start, unit.start + unit.size),
                    (execution, execution + unit.size),
                    "resource" if kind == "resource" else "host",
                )
                provider = "source:" + cache.key(project.id, source_path)
                entities.setdefault(provider, Entity(provider, "provider", source_path, path=source_path))
                families.append(Family("provider:" + provider, provider, (provider, key), "producer binding"))
    try:
        modules = module_map.load(project)
        for group in modules.groups:
            if group.signals == ("cap",) or (group.evidence == "inferred" and not set(group.signals) - {"cap"}):
                continue
            for version in project.versions:
                members = tuple(functions[version, name] for name in group.members if (version, name) in functions)
                if not members:
                    continue
                seed = "module:" + cache.key(project.id, version, group.header)
                entities[seed] = Entity(seed, "module", group.name, version, group.header)
                families.append(Family(seed, seed, tuple(sorted((seed, *members))), "layout module"))
    except (Held, OSError):
        missing.append("layout modules unavailable")
    graph = Graph.capture(project)
    provider_members: dict[str, set[str]] = defaultdict(set)
    for (version, name), key in sorted(functions.items()):
        sources = [project.src / (name + ".c"), project.work / name / (name + ".c")]
        source = next((path for path in sources if path.is_file()), None)
        if source is None:
            missing.append(f"{version}:{name}:source unavailable")
            continue
        closure = graph.closure((source,))
        if closure.unknown:
            missing.append(f"{version}:{name}:include closure unresolved")
        source_ref = source.relative_to(project.root).as_posix()
        source_digest = inputs.digest(source, algorithm="sha256", reuse=cache.configured())
        refs.append(cache.key(source_ref, source_digest))
        for path in closure.paths:
            if path == source or not path.is_relative_to(project.root):
                continue
            relative = path.relative_to(project.root).as_posix()
            provider = "source:" + cache.key(project.id, relative)
            entities.setdefault(provider, Entity(provider, "provider", relative, path=relative))
            provider_members[provider].add(key)
            digest = inputs.digest(path, algorithm="sha256", reuse=cache.configured())
            correlation = cache.key(provider, key)
            evidence.append(
                Evidence(
                    "include:" + correlation,
                    "provider_include",
                    (provider, key),
                    source_ref,
                    cache.key(source_digest, digest),
                    correlation,
                    strength=5,
                    propagate=True,
                    unresolved_reason="conditional or unresolved include closure" if closure.unknown else None,
                )
            )
    for provider, consumers in sorted(provider_members.items()):
        families.append(
            Family("provider:" + provider, provider, tuple(sorted((provider, *consumers))), "retained include Graph")
        )
    try:
        mapped = mapping.load_map(project)
        refs.append(cache.key(cache.serialized(mapped["inputs_sha256"]), str(mapped["shard"])))
        for (version, name), key in sorted(functions.items()):
            body = mapped["functions"][name]["versions"].get(version)
            if body is None:
                continue
            for index, call in enumerate(body["calls"]):
                target = functions.get((version, call.get("callee")))
                correlation = cache.key(key, str(call.get("instruction", index)))
                evidence.append(
                    Evidence(
                        "call:" + correlation,
                        "direct_call",
                        (target, key) if target else (key,),
                        f"map:{version}:{name}:{index}",
                        body["target_sha256"],
                        correlation,
                        strength=3,
                        propagate=target is not None,
                        unresolved_reason=None if target else "call target unresolved in retained projection",
                    )
                )
    except (Held, OSError, KeyError):
        missing.append("current ROM map unavailable")
    return Facts(tuple(entities.values()), tuple(evidence), tuple(families), tuple(missing), tuple(refs))


def snapshot(
    project: Project, available_facts: Facts | None = None, *, subjects: frozenset[str] | None = None
) -> Snapshot:
    """Optional cache failures never hold planning, native comparison, or publication."""
    if available_facts is None:
        try:
            available_facts = retained(project, subjects=subjects)
        except (Held, OSError, ValueError, KeyError) as error:
            available_facts = Facts(missing_inputs=(f"retained subsystem facts unavailable: {type(error).__name__}",))
    facts = canonical(available_facts)
    _, evidence_key = _keys(facts)
    try:
        result = cache.Cache(project.cache).value("subsystems", evidence_key, cache.PICKLE, lambda: aggregate(facts))
        if not isinstance(result, Snapshot) or result.evidence_key != evidence_key or result.inference_recipe != RECIPE:
            return aggregate(facts)
        return result
    except (Held, OSError, ValueError):
        return aggregate(facts)
