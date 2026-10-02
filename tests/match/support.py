"""Match behavior with stored trial proofs and a controlled build implementation."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import unbake.decomp.drafts as drafts
import unbake.decomp.needs as needs
import unbake.match.queue as match
from unbake.decomp.trial import Trial
from unbake.decomp.trial_compare import Compare
from unbake.project import build
from unbake.project.config import Compiler, Held, Policy, Project, Version
from unbake.report import progress

SCRATCH_ROOT = Path(tempfile.gettempdir())


class MatchFixture(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("feedback", "feedback_many"):
            feedback = patch("unbake.decomp.type_context." + name)
            feedback.start()
            self.addCleanup(feedback.stop)
        (SCRATCH_ROOT).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.sources = Path(self.temporary.name) / "submitted"
        self.sources.mkdir()
        self.src = self.root / "src"
        self.src.mkdir()
        (self.root / "config.toml").write_text(
            '[build]\nld="ld"\nobjcopy="objcopy"\nsplat="splat"\nas="as"\nasflags=[]\n'
            'cpp="policy:cpp"\ncppflags=[]\nsn64_asflags=[]\n'
        )
        self.versions = ("us", "eu")
        version_map = {}
        self.original = {}
        for name in self.versions:
            directory = self.root / "versions" / name
            directory.mkdir(parents=True)
            split = directory / "fixture.yaml"
            split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x1000\n"
                "    vram: 0x80001000\n    subsegments:\n      - [0x1000, asm, text/alpha]\n"
                "      - [0x1010, asm, beta]\n      - [0x1020, asm, gamma]\n"
                "      - [0x1030, data, constants]\n  - [0x1040]\n"
            )
            symbols = directory / "symbol_addrs.txt"
            symbols.write_text("alpha = 0x80001000;\nbeta = 0x80001010;\ngamma = 0x80001020;\n")
            baserom = self.root / f"roms/baserom.{name}.z64"
            baserom.parent.mkdir(exist_ok=True)
            baserom.write_bytes(bytes(range(64)))
            version_map[name] = Version(name, baserom, "a" * 40, split, symbols, ())
            version_map[name].baserom.write_bytes(bytes(0x1050))
            generation = self.root / "build" / f"{name}.0"
            generation.mkdir(parents=True)
            (generation / ".inuse").touch()
            (generation / "object.o").write_bytes(b"original immutable output")
            for unit in ("text/alpha", "beta", "gamma"):
                target = generation / "obj/asm" / (unit + ".o")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"pinned target")
            (self.root / "build" / name).symlink_to(generation.name)
            self.original[name] = generation
        tools = self.root / "tools"
        tools.mkdir()
        include = self.root / "include"
        include.mkdir()
        (include / "types.h").write_text("typedef int word;\n")
        asm = self.root / "asm"
        asm.mkdir()
        for filename in ("cc", "as", "compiler.sha256"):
            (tools / filename).write_bytes(b"fixture compiler pins")
        compiler = Compiler("gcc-2.8.1-sn64", "sn64", tools / "cc", tools / "as", (), tools / "compiler.sha256")
        self.project = Project(
            root=self.root,
            name="fixture",
            title="Fixture",
            names_from="us",
            versions=self.versions,
            src=self.src,
            include=(include,),
            asm=asm,
            tools=tools,
            compilers={compiler.id: compiler},
            default_compiler=compiler.id,
            units={},
            version_map=version_map,
            id="00000000-0000-4000-8000-000000000001",
            workspace_id="00000000-0000-4000-8000-000000000002",
            roms=self.root / "roms",
            build=self.root / "build",
            work=self.root / "build/work",
            drafts=self.root / "build/drafts",
        )
        self.policy = Policy(
            cores=2,
            stall_trials=4,
            assignment_idle_hours=1.0,
            cache_root=Path(self.temporary.name) / "cache",
            state_root=Path(self.temporary.name) / "state",
            objdiff_cli=tools / "objdiff",
            objdiff_sha256="b" * 64,
            m2c=tools / "m2c",
            splat=tools / "splat",
            mips_ld=tools / "ld",
            mips_objdump=tools / "objdump",
            mips_readelf=tools / "readelf",
            same_game_similarity=0.10,
            probe_count=20,
            mips_as=tools / "as",
            mips_objcopy=tools / "objcopy",
            cpp=Path(shutil.which("cpp") or "cpp"),
            asflags=(),
            cppflags=(),
            sn64_asflags=(),
            search_beam=2,
            permuter_archive=tools / "permuter.tar",
            permuter_sha256="c" * 64,
        )
        self.store = drafts.Store(self.policy, self.project)
        (self.root / "Makefile").write_text("all:\n\ttrue\n")
        self.calls: list[tuple[str, ...]] = []
        self.build_failures: set[tuple[str, str]] = set()
        self.interactions: list[set[str]] = []
        self.on_build: Callable[[Path, Callable[[str], Path]], object] | None = None
        self.addCleanup(patch.stopall)
        patch.object(progress, "write", return_value=[]).start()
        patch.object(progress, "measure", return_value={}).start()
        patch.object(build, "compile_object", return_value=Path("unused.o")).start()
        patch.object(build, "build", self.build, create=True).start()
        patch.object(build, "current_generation", self.current, create=True).start()

    def current(self, project: Project, version: str) -> Path:
        link = project.build_link(version)
        if not link.is_symlink() or not link.resolve().is_dir():
            raise Held("build", f"{link}: current generation missing")
        return link.resolve()

    def build(
        self,
        project: Project,
        policy: Policy,
        versions: list[str],
        *,
        tree: Path,
        generation_for: Callable[[str], Path],
    ) -> dict[str, SimpleNamespace]:
        functions = sorted(path.stem for path in (tree / "src").glob("*.c") if not drafts.is_partial(path.read_text()))
        self.calls.append(tuple(functions))
        self.assertTrue(tree.is_relative_to(self.root / "build" / "match"))
        self.assertFalse((tree / "build").exists())
        for function in functions:
            for version in versions:
                split = (tree / project.version(version).split.relative_to(self.root)).read_text()
                self.assertIn(f", c, {function}]", split)
        inputs = {v: self.current(project, v) for v in versions}
        if self.on_build:
            self.on_build(tree, generation_for)
        results = {}
        for version in versions:
            generation = generation_for(version)
            self.assertEqual(generation.parent, self.root / "build")
            self.assertNotEqual(generation, self.current(project, version))
            self.assertEqual((generation / "object.o").read_bytes(), (inputs[version] / "object.o").read_bytes())
            self.assertNotEqual((generation / "object.o").stat().st_ino, (inputs[version] / "object.o").stat().st_ino)
            (generation / "object.o").write_bytes(("compiled " + ",".join(functions)).encode())
            log = generation / "build.log"
            log.write_text("compare\n")
            ok = not any((function, version) in self.build_failures for function in functions)
            ok = ok and not any(group <= set(functions) for group in self.interactions)
            results[version] = SimpleNamespace(
                version=version, ok=ok, sha1_line="fixture: OK" if ok else "FAIL", log=log, generation=generation
            )
        return results

    def draft(
        self,
        function: str,
        content: str | None = None,
        *,
        identical: bool = True,
        versions: Iterable[str] | None = None,
        pending: list[needs.Need] | None = None,
    ) -> Path:
        source = self.sources / f"{function}.c"
        source.write_text(content if content is not None else f"int {function}(void) {{ return 0; }}\n")
        self.prove(source, identical=identical, versions=versions, pending=pending)
        return source

    def prove(
        self,
        source: Path,
        *,
        identical: bool = True,
        versions: Iterable[str] | None = None,
        pending: list[needs.Need] | None = None,
    ) -> None:
        selected = match.holding_versions(self.project, source.stem) if versions is None else tuple(versions)
        from unbake.layout import split

        for version in selected:
            _, _, segments = split.layout(self.project.version(version).split)
            for segment in segments:
                for row in segment.rows:
                    if row.kind in ("asm", "c") and Path(row.path).name == source.stem:
                        target = (
                            self.original[version] / "obj" / ("src" if row.kind == "c" else "asm") / (row.path + ".o")
                        )
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"pinned target")
        typed = dict.fromkeys(("register", "order", "immediate", "relocation", "inserted", "missing", "changed"), 0)
        trial = Trial(
            function=source.stem,
            source_sha256=drafts.source_identity(source.read_bytes()),
            preconditions=[],
            next_command="match submit " + source.name,
            compares={
                v: Compare(
                    version=v,
                    identical=4 if identical else 3,
                    of=4,
                    typed=typed.copy(),
                    lines=[],
                    match_percent=100 if identical else 75,
                    register_changes=(),
                )
                for v in selected
            },
        )
        self.store.add(trial, source, {v: 100 if identical else 75 for v in selected})

    def remove_proofs(self, function: str) -> None:
        rows = [row for row in self.store.history() if row["function"] != function]
        (self.store.root / "trials.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def queue(self, *functions: str) -> None:
        for function in functions:
            match.submit(self.project, self.policy, self.draft(function))

    def queued(self) -> list[dict[str, Any]]:
        path = self.root / ".unbake" / "state" / "match-queue.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def matched(self) -> list[dict[str, Any]]:
        path = self.policy.state_root / self.project.id / self.project.workspace_id / "receipts" / "match.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_untouched(self) -> None:
        self.assertFalse(list(self.src.glob("*.c")))
        self.assertEqual(self.matched(), [])
        for version in self.versions:
            self.assertEqual(self.current(self.project, version), self.original[version])
            self.assertEqual((self.original[version] / "object.o").read_bytes(), b"original immutable output")
            self.assertNotIn(", c, ", self.project.version(version).split.read_text())
