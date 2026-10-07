"""Compiler-neutral discovery of uniquely proved native runtime helper bindings."""

from pathlib import Path

from unbake.config import Held, Project


def bindings(project: Project, version: str) -> dict[str, int]:
    from unbake import inputs
    from unbake.cache import memo
    from unbake.compilers.families import family_named
    from unbake.compilers.registry import REGISTRY_PATH, specification
    from unbake.decomp.rom import project_reader
    from unbake.layout import split

    configured = project.version(version)
    families = sorted({specification(c.id).family for c in project.compilers.values()})
    sources = tuple(sorted(Path(__file__).parent.rglob("*.py")))

    def discover() -> dict[str, int]:
        reader = project_reader(project, version)
        _, _, segments = split.layout(configured.split)
        matches: dict[str, set[int]] = {}
        for segment in segments:
            if segment.end is None or "start" not in segment.fields or "vram" not in segment.fields:
                continue
            start = split.number(segment.fields["start"], "segment start")
            address = split.number(segment.fields["vram"], "segment vram")
            with configured.baserom.open("rb") as stream:
                stream.seek(start)
                data = stream.read(segment.end - start)
            for name in families:
                for offset, helper in family_named(name).runtime_helpers(data, reader):
                    matches.setdefault(helper.name, set()).add(address + offset)
        ambiguous = [name for name, addresses in matches.items() if len(addresses) != 1]
        if ambiguous:
            raise Held("link", f"link.runtime_identity: nonunique native helpers: {', '.join(sorted(ambiguous))}")
        return {name: next(iter(addresses)) for name, addresses in matches.items()}

    return memo(
        "compiler.runtime-bindings",
        (
            configured.baserom,
            inputs.signature(configured.baserom),
            configured.split,
            inputs.signature(configured.split),
            tuple(project.resident_mappings.get(version, ())),
            tuple(families),
            REGISTRY_PATH,
            inputs.signature(REGISTRY_PATH),
            tuple((p, inputs.signature(p)) for p in sources),
        ),
        discover,
        keep=len(project.versions),
    )
