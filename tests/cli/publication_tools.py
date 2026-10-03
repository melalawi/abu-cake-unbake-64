"""Canned compiler/linker artifacts for publication transaction unit tests."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.elf_fixture import write_object
from tests.preprocessor import expand, output
from tests.process_fakes import boundary, copy
from unbake.decomp import drafts, trial, trial_compile
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.layout import entries, structs
from unbake.match import relink, staging
from unbake.project import build, hygiene, setup, toolchain
from unbake.project_tools import extract
from unbake.report import progress
from unbake.typemap import declarations

CODE = bytes.fromhex("03e0000824020001")


class Tools:
    def __init__(self, case):
        self.case = case
        case.project.tools.mkdir(parents=True, exist_ok=True)
        (case.project.tools / "compiler.sha256").write_text("")
        setup.refresh_helpers(case.project)
        self.bad = set()
        self.proof_hook = None
        self.built = []
        self.compiled = []
        self.linked = []
        for mock in (
            boundary(progress, self.report),
            patch.object(toolchain, "ensure"),
            patch.object(toolchain, "verify", return_value={}),
            boundary(
                hygiene, lambda command, **kwargs: __import__("subprocess").CompletedProcess(command, 0, b"", b"")
            ),
            boundary(staging, copy),
            boundary(declarations, output),
            boundary(structs, output),
            boundary(entries, output),
            boundary(trial_compile, self.frontend),
            patch.object(build, "compile_objects", side_effect=self.compile),
            patch.object(build, "preprocess_object", side_effect=self.preprocess),
            patch.object(build, "build", side_effect=self.build),
            patch.object(relink, "prove", side_effect=self.prove),
            patch.object(trial, "retain_draft", side_effect=self.try_source),
        ):
            mock.start()
            case.addCleanup(mock.stop)

    def report(self, command, **kwargs):
        import subprocess

        workspace = Path(command[command.index("--project") + 1])
        units = json.loads((workspace / "objdiff.json").read_bytes())["units"]
        measures = dict(matched_code=0, complete_code=0, total_code=0, complete_units=0, total_units=len(units))
        Path(command[command.index("--output") + 1]).write_text(
            json.dumps(dict(version=2, measures=measures, units=units))
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def frontend(self, command, **kwargs):
        import subprocess

        if "-c" in command or "-quiet" in command:
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_bytes(b"frontend fixture")
            return subprocess.CompletedProcess(command, 0, "", "")
        return output(command, **kwargs)

    def preprocess(self, project, policy, source, version):
        return expand(source.read_text(), project.include).encode()

    def try_source(self, project, policy, source, scratch, versions=None, **kwargs):
        from unbake.layout import split

        selected = versions or split.holding_versions(project, source.stem)
        result = trial.Trial(
            source.stem,
            drafts.source_identity(source.read_bytes()),
            {
                v: Compare(
                    v,
                    2,
                    2,
                    dict.fromkeys(TYPES, 0),
                    [f"VERSION {v}: identical 2 of 2 instructions; objdiff 100.000000%"],
                    100.0,
                    (),
                )
                for v in selected
            },
            [],
            "unbake submit " + str(source),
        )
        result.work_identity = {"schema": 1, "project_id": project.id, "workspace_id": project.workspace_id}
        drafts.Store(policy, project).add(result, source, {v: 100.0 for v in selected})
        return result

    def prime(self, project, version, generation):
        generation.mkdir(parents=True, exist_ok=True)
        (generation / f"{project.name}.{version}.z64").write_bytes(project.version(version).baserom.read_bytes())
        (generation / "obj/asm").mkdir(parents=True, exist_ok=True)
        (generation / "obj/src").mkdir(parents=True, exist_ok=True)
        (generation / "obj/assets").mkdir(parents=True, exist_ok=True)
        from unbake.layout import split

        ranges = {row.name: {"address": row.address} for row in split.functions(project, version)}
        script, asm = [], []
        dump = ["name,vram_start,subsegment_type\n"]
        for name, row in ranges.items():
            relative = f"obj/asm/{name}.o"
            write_object(generation / relative, {".text": CODE}, [(name, ".text", 0, 8)])
            script.append(f"{relative}(.text)")
            asm.append(f"$(BUILD)/{relative}")
            dump.append(f"{name},{row['address']:X},asm\n")
        (generation / f"{project.name}.ld").write_text("SECTIONS { .text : { " + " ".join(script) + " } }\n")
        (generation / ".split.mk").write_text(
            "C_OBJECTS := \nASM_OBJECTS := " + " ".join(asm) + "\nASSET_OBJECTS := \nLINK_SCRIPTS := \n"
        )
        (generation / "unit-ranges.json").write_text(
            json.dumps(extract.unit_ranges(project.version(version).split.read_text()), sort_keys=True)
        )
        (generation / "splat_symbols.csv").write_text("".join(dump))
        (generation / "committed_symbols.ld").write_text(
            "".join(f"PROVIDE({name} = 0x{row['address']:08X});\n" for name, row in ranges.items())
        )
        (generation / "symbol-addresses.txt").write_text(
            "".join(f"{name} 0x{row['address']:08X}\n" for name, row in ranges.items())
        )
        for filename in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
            (generation / filename).write_text("")

    def compile(self, project, policy, sources, version, out, *, cancel_file=None):
        self.compiled.append((version, tuple(source.stem for source in sources)))
        failures = {}
        for source in sources:
            if cancel_file is not None and cancel_file.exists():
                break
            if "invalid C" in source.read_text():
                failures[source.stem] = "invalid C"
                if cancel_file is not None:
                    cancel_file.touch()
                    break
                continue
            import re

            definitions = re.findall(r"\bint\s+(\w+)\(void\)\s*\{", source.read_text())
            code = CODE * max(1, len(definitions))
            header = project.include[0] / "value.h"
            if "return 2" in source.read_text() or (header.is_file() and "PROOF_VALUE 2" in header.read_text()):
                code = CODE[:-1] + b"\x02"
            target = out / (source.stem + ".o")
            target.unlink(missing_ok=True)  # The production compiler publishes by atomic replacement.
            if "missing()" in source.read_text():
                write_object(
                    target,
                    {".text": bytes.fromhex("0c00000000000000")},
                    [(source.stem, ".text", 0, 8), ("missing", None, 0, 0, 0x12)],
                    relocations=[(".text", 0, 4, "missing")],
                )
            else:
                write_object(
                    target,
                    {".text": code},
                    [(name, ".text", i * 8, 8) for i, name in enumerate(definitions or [source.stem])],
                )
            target.with_suffix(".built").touch()
            dependencies = [source, *project.include[0].rglob("*.h")]
            relative = [path.relative_to(project.root).as_posix() for path in dependencies]
            target.with_suffix(".d").write_text(f"{target}: " + " ".join(relative) + "\n")
            target.with_suffix(".inputs.json").write_text(
                json.dumps(
                    {
                        name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for name, path in zip(relative, dependencies, strict=True)
                    }
                )
            )
        return failures

    def build(self, project, policy, versions, *, tree, generation_for):
        self.built.append(tuple(versions))
        staged = staging.project_at(project, tree)
        results = {}
        for version in versions:
            generation = generation_for(version)
            self.prime(staged, version, generation)
            sources = list(staged.src.glob("*.c"))
            self.compile(staged, policy, sources, version, generation / "obj/src")
            results[version] = self.prove(staged, policy, version, generation, None)
        return results

    def prove(self, project, policy, version, generation, retained, **kwargs):
        from unbake.project_tools.elf import Object

        ranges = extract.unit_ranges(project.version(version).split.read_text())
        names = [name for name in ranges if (project.src / (name + ".c")).is_file()]
        self.linked.append((version, tuple(names)))
        if self.proof_hook:
            self.proof_hook(project, names)
        failures = []
        for name in names:
            path = generation / "obj/src" / (name + ".o")
            if name in self.bad:
                path.unlink(missing_ok=True)
                write_object(path, {".text": CODE[:-1] + b"\x02"}, [(name, ".text", 0, 8)])
            if not path.is_file():
                failures.append(name)
                continue
            obj = Object(path)
            if obj.content(obj.section(".text")) != CODE * ((ranges[name]["end"] - ranges[name]["start"]) // 8):
                failures.append(name)
        ok = not failures and Path(policy.mips_ld).name != "refuse-link"
        log = generation / "build.log"
        log.write_text(
            "".join(
                f"src/{name}.c: invalid C\n"
                for name in names
                if "invalid C" in (project.src / (name + ".c")).read_text()
            )
            + "".join(f"HELD(link): obj/src/{name}.o .text symbol {name}: byte mismatch\n" for name in failures)
        )
        image = generation / f"{project.name}.{version}.z64"
        image.write_bytes(project.version(version).baserom.read_bytes() if ok else b"wrong")
        return build.BuildResult(version, ok, str(image) + (": OK" if ok else ": FAILED"), log, generation)
