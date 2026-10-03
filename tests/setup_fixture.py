"""Canned disassembler/compiler output for setup transaction unit tests."""

import struct
import subprocess
import zlib
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import assemble, assembly
from tests.preprocessor import output
from tests.process_fakes import boundary, copy, git_init
from unbake.layout import planner, split, split_create
from unbake.match import staging
from unbake.project import build, header, hygiene, init, setup_proof, toolchain
from unbake.typemap import declarations


def measured(image, yaml, executable, work, version):
    # These cartridges explicitly seed entries with boot JAL words.
    starts = {0x1000}
    for (word,) in struct.iter_unpack(">I", image[0x1000:0x1100]):
        if word >> 26 == 3:
            starts.add((word & 0x3FFFFFF) << 2)
    functions = []
    for start in sorted(starts):
        end = next(
            (
                at + 8
                for at in range(start, min(start + 0x100, len(image) - 4), 4)
                if struct.unpack_from(">I", image, at)[0] == 0x03E00008
            ),
            start + 8,
        )
        name = f"func_{0x80000000 + start:08X}"
        functions.append(split.Function(version, name, start, end, 0x80000000 + start, name, "asm", ()))
    return split.ExtractedText(functions, ())


def extraction(project, version, cores, *, log, slots=None):
    generation = project.build_link(version)
    from unbake.project_tools.extract import prepare_build

    prepare_build(generation)
    owners = split.functions(project, version)
    image = project.version(version).baserom.read_bytes()
    paths, cpaths = [], []
    for owner in owners:
        directory = generation / "obj/asm" / Path(owner.path).parent
        directory.mkdir(parents=True, exist_ok=True)
        text = assembly(
            owner.name, list(struct.unpack(f">{(owner.end - owner.start) // 4}I", image[owner.start : owner.end]))
        )
        obj = assemble(directory, Path(owner.path).name, text)
        asm = project.asm / version / (owner.path + ".s")
        asm.parent.mkdir(parents=True, exist_ok=True)
        asm.write_text(text)
        paths.append(obj.relative_to(generation))
        if owner.kind == "c":
            target = generation / "obj/src" / (owner.path + ".o")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(obj.read_bytes())
            target.with_suffix(".built").touch()
            cpaths.append(target.relative_to(generation))
    import json

    from unbake.project_tools.extract import unit_ranges

    (generation / "unit-ranges.json").write_text(json.dumps(unit_ranges(project.version(version).split.read_text())))
    (generation / "symbol-addresses.txt").write_text(
        "".join(
            f"{name} {address:#x}\n"
            for name, (address, _, _) in split.symbols(project.version(version).symbols)[1].items()
        )
    )
    (generation / ".split.mk").write_text(
        "ASM_OBJECTS := "
        + " ".join("$(BUILD)/" + str(path) for path in paths)
        + "\nC_OBJECTS := "
        + " ".join("$(BUILD)/" + str(path) for path in cpaths)
        + "\n"
    )
    from tests.elf_fixture import write_object

    write_object(
        generation / f"{project.name}.elf",
        {".text": image[0x1000:0x2200]},
        [(owner.name, ".text", owner.address, owner.end - owner.start) for owner in owners],
        addresses={".text": 0x80001000},
        linked=True,
    )
    (generation / ".inuse").touch()
    (generation / f"{project.name}.ld").write_text(
        "SECTIONS { .text : { " + " ".join(f"{p}(.text)" for p in paths) + " } }"
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.with_suffix(".extract.log").write_text("fixture extraction\n")


def proof(project, version, data, cores, *, log=None, slots=None, extracted=False):
    from unbake.project.config import Held

    if any("return 17" in source.read_text() for source in project.src.rglob("*.c")):
        raise Held("setup", f"setup.sha1.{version}: ROM mismatch at first differing offset 0x1100")
    if not extracted:
        extraction(project, version, cores, log=log or project.build / "setup" / f"{version}.log", slots=slots)
    image = data.read_bytes() if isinstance(data, Path) else data
    (project.build_link(version) / f"{project.name}.{version}.z64").write_bytes(image)


def compile_object(project, policy, source, version, destination):
    owner = next(row for row in split.functions(project, version) if row.name == source.stem)
    image = project.version(version).baserom.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    return assemble(
        destination.parent,
        destination.stem,
        assembly(
            owner.name, list(struct.unpack(f">{(owner.end - owner.start) // 4}I", image[owner.start : owner.end]))
        ),
    )


def ensure(project, policy, **kwargs):
    project.tools.mkdir(parents=True, exist_ok=True)
    (project.tools / "compiler.sha256").write_text("")
    for compiler in project.compilers.values():
        compiler.cc.parent.mkdir(parents=True, exist_ok=True)
        compiler.cc.write_bytes(b"fixture compiler")
        compiler.as_.write_bytes(b"fixture assembler")
        compiler.sha256.parent.mkdir(parents=True, exist_ok=True)
        compiler.sha256.write_text("")


def install(case):
    from tests.objdiff_fixture import install as objdiff

    objdiff(case)
    from tests.splat_fixture import create

    for mock in (
        boundary(split_create, create),
        patch.dict(header.RETAIL, {zlib.crc32(bytes(0xFC0)): "6102/7101"}),
        boundary(init, git_init),
        boundary(staging, copy),
        boundary(hygiene, lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"", b"")),
        boundary(declarations, output),
        patch.object(planner, "measure", side_effect=measured),
        patch.object(toolchain, "ensure", side_effect=ensure),
        patch.object(toolchain, "verify", return_value={}),
        patch.object(setup_proof, "extract", side_effect=extraction),
        patch.object(setup_proof, "proof", side_effect=proof),
        patch.object(build, "compile_object", side_effect=compile_object),
    ):
        mock.start()
        case.addCleanup(mock.stop)
