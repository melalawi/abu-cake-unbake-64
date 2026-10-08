"""Digest-checked retained artifacts; native replay never runs a compiler."""

import gzip
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import make
from unbake import runner
from unbake.compilers import drivers
from unbake.compilers.recipe_options import recipe_digest
from unbake.compilers.registry import compiler_directory, specification

FIXTURE = Path(__file__).parents[1] / "fixtures/unified_options"
B = "func_800B1520_us"


def retained(name):
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    row = next(r for r in manifest["payloads"] if r["path"] == name + ".gz")
    content = gzip.decompress((FIXTURE / row["path"]).read_bytes())
    assert hashlib.sha256(content).hexdigest() == row["sha256"], name
    assert len(content) == row["bytes"], name
    return content


def phases(**values):
    return {p: list(values.get(p, ())) for p in ("preprocess", "compile", "assemble", "link")}


def gcc_project(root, *, function=B, content=None, target=None):
    project, host = make(root, versions=("us",))
    spec = specification("gcc-2.8.1-sn64")
    directory = compiler_directory(project.tools, spec)
    directory.mkdir(parents=True)
    # Counterfactual process-boundary files: these are NOT historical binaries.
    (directory / spec.cc).write_bytes(b"mock native boundary; not a compiler")
    compiler = replace(
        project.compilers["ido-7.1"],
        id=spec.id,
        kind=spec.kind,
        cc=directory / spec.cc,
        as_=Path(spec.as_),
        cflags=spec.cflags,
    )
    project = replace(
        project, compilers={spec.id: compiler}, default_compiler=spec.id, gnu_asflags=("-EB", "-mips3", "-G0")
    )
    target = retained("bt-target.bin") if target is None else target
    project.version("us").baserom.write_bytes(bytes(0x40) + target)
    project.version("us").split.write_text(
        "segments:\n  - name: main\n    type: code\n    start: 0x40\n    vram: 0x800B1520\n"
        f"    subsegments:\n      - [0x40, asm, {function}]\n  - [0x{0x40 + len(target):X}]\n"
    )
    file = project.src / (function + ".c")
    file.write_text(retained("bt-baseline.c").decode() if content is None else content)
    # Native compilation alone is replayed; dependency scanning still reads actual headers.
    from tests.work.test_creative_slim import FIXTURES

    for name in ("types.h", "vec3.h"):
        (project.include[-1] / name).write_bytes((FIXTURES / name).read_bytes())
    return project, host, file


def expanded_baseline():
    import re

    return re.sub(r"^\s*#.*$", "", retained("bt-baseline.i").decode(), flags=re.M)


@contextmanager
def native_replay(project, provider, calls):
    """Only compilation/link process boundaries are mocked; measurement stays real."""

    @contextmanager
    def compile_unit(view, host, file, version, *, unit, capture_info, **kwargs):
        recipe = drivers.resolved(view, version, unit)
        calls.append((file.read_text(), recipe))
        obj = project.build / "replayed.o"
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(retained("bt-exact.o"))
        capture_info.update(
            preprocessed_sha256=hashlib.sha256(retained("bt-exact.i")).hexdigest(),
            object_sha256=hashlib.sha256(obj.read_bytes()).hexdigest(),
            compiler_pins=recipe_digest(dict(recipe.pins)),
            compile_argv=["retained-native-boundary", *recipe.phase("compile")],
        )
        yield obj

    def link(view, host, obj, version, row, file, *, capture_info):
        linked = provider(file.read_text(), drivers.resolved(view, version, file.stem))
        capture_info.update(
            placed_object_sha256=hashlib.sha256(obj.read_bytes()).hexdigest(),
            linked_sha256=hashlib.sha256(linked).hexdigest(),
            placement={"address": row.address, "start": row.start, "end": row.end},
            placement_refusals=[],
        )
        return linked, []

    with patch.object(runner, "compile_unit", compile_unit), patch.object(runner, "link_function", link):
        yield
