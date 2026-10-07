"""The unchanged registry candidate reaches real public comparison through its includes."""

import hashlib
import json
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from tests.fold.test_owned_contract import FIVE, FIXTURE
from tests.project_fixture import ProjectCase
from unbake import cdecl, config, land, pool, runner
from unbake.config import Held
from unbake.layout import index
from unbake.layout import map as ownership
from unbake.process import named
from unbake.typemap import declaration_evidence
from unbake.work import compare
from unbake.work.attempts import Attempt

RECORDS = json.loads((FIXTURE / "records.json").read_text())
PROGRAMS = json.loads((FIXTURE / "machine.json").read_text())["programs"][FIVE]
HOME = "span_1000/code_8025A3EC.h"


class OwningPublicCompareTests(ProjectCase):
    versions = tuple(RECORDS["holders"])

    def setUp(self):
        super().setUp()
        for version in self.versions:
            row = self.project.version(version)
            packet = PROGRAMS[version]
            data = bytes.fromhex(packet["hex"])
            row.baserom.write_bytes(bytes.fromhex("80371240") + bytes(0x3C) + data)
            row.split.write_text(
                "name: fixture\nsegments:\n  - [0x0, header, header]\n"
                "  - name: span_1000\n    type: code\n    start: 0x40\n"
                f"    vram: {packet['address']}\n    subsegments:\n      - [0x40, asm, {FIVE}]\n"
                f"  - [{0x40 + len(data)}]\n"
            )
            row.symbols.write_text(f"{FIVE} = {packet['address']};\n")
        layout = self.project.root / "layout.toml"
        layout.write_text(layout.read_text().replace("alpha", FIVE))
        self.project = config.load(self.project.root)
        self.file = self.project.work / FIVE / (FIVE + ".c")
        self.file.parent.mkdir(parents=True)
        self.source = (FIXTURE / self.file.name).read_text()
        self.file.write_text(self.source)
        self.private = self.file.parent / "include"
        self.header = self.project.include[0] / HOME
        self.old = "#ifndef UNBAKE_SPAN_1000_REGISTRY_H\n#define UNBAKE_SPAN_1000_REGISTRY_H\n"
        self.old += RECORDS["5B920_old_header"] + "#endif\n"
        primitive, registry = (FIXTURE / "5B920-context.h").read_text().split("typedef struct {", 1)
        (self.project.include[0] / "types.h").write_text(primitive)
        for root in (self.private, self.project.include[0]):
            for name, text in {
                HOME: self.old,
                "shared/func_8025B920_de_closed.h": (
                    '#include "registry-context.h"\n#include "span_1000/code_8025A3EC.h"\n'
                ),
                "registry-context.h": '#include "types.h"\ntypedef struct {' + registry,
            }.items():
                if root == self.project.include[0] and name != HOME:
                    continue
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        index.path(self.project).parent.mkdir(parents=True, exist_ok=True)
        index.path(self.project).write_bytes(
            index.encoded(
                {
                    "schema": 1,
                    "headers": {HOME: hashlib.sha256(self.old.encode()).hexdigest()},
                    "symbols": {FIVE: HOME},
                    "clusters": {},
                    "type_headers": {},
                }
            )
        )
        self.compiler = shutil.which("cc")
        self.assertIsNotNone(self.compiler)
        self.syntax = []
        self.measured = []

    @contextmanager
    def compile(self, project, host, source, version, *, unit, **options):
        result = subprocess.run(
            [
                self.compiler,
                "-std=gnu89",
                "-fsyntax-only",
                *("-I" + str(root) for root in project.include),
                str(source),
            ],
            capture_output=True,
            text=True,
        )
        self.syntax.append((version, result.returncode))
        if result.returncode:
            raise Held(named("compile.cc1", result.stderr, owner="fixture", stage="compile"))
        yield self.root / "fixture.o"

    def test_public_compare_keeps_proposed_headers_through_measure_and_stops_before_land(self):
        real_compare = compare.compare

        def measured(*args, **kwargs):
            result = real_compare(*args, **kwargs)
            self.measured.append(result)
            return result

        def after_comparison(*args, **kwargs):
            self.assertEqual(len(self.measured), 1)
            self.assertTrue(self.measured[0].exact, self.measured[0].lines())
            raise Held(named("fixture.after-comparison", "stop before publication", owner="fixture", stage="land"))

        with (
            patch.object(
                pool,
                "run",
                side_effect=lambda host, fn, jobs, shared=None: [
                    fn(job) if shared is None else fn(shared, job) for job in jobs
                ],
            ),
            patch.object(runner, "compile_unit", side_effect=self.compile),
            patch.object(
                runner,
                "link_function",
                side_effect=lambda p, h, o, version, row, file: (bytes.fromhex(PROGRAMS[version]["hex"]), []),
            ),
            patch.object(compare, "compare", side_effect=measured),
            patch("unbake.work.compare_facts.attach"),
            patch.object(land, "land", side_effect=after_comparison) as publication,
        ):
            result = land.publish(self.project, self.host, [self.file], compare_first=True)
        self.assertEqual(result.failed[FIVE]["key"], "fixture.after-comparison")
        self.assertEqual(publication.call_count, 1)
        self.assertEqual(self.syntax, [(version, 0) for version in self.versions])
        self.assertEqual(self.file.read_text(), self.source)
        self.assertEqual(hashlib.sha256(self.file.read_bytes()).hexdigest(), RECORDS["source_sha256"][self.file.name])
        self.assertEqual(self.header.read_text(), self.old)
        self.assertEqual((self.private / HOME).read_text(), self.old)
        self.assertFalse((self.project.src / self.file.name).exists())
        self.assertEqual(result.commits, [])

    def test_plain_draft_view_still_selects_its_private_provider(self):
        view = compare.view_for(self.project, self.file, FIVE)
        self.assertEqual(view.work_include, (self.private,))

    def test_explicit_view_is_preserved_for_every_comparison_preparation(self):
        roots = (self.root / "proposal", self.root / "second")
        view = replace(self.project, work_include=roots)
        self.assertEqual(compare.view_for(view, self.file, FIVE).work_include, roots)

    def test_ordinary_comparison_stages_the_own_definition_without_changing_old_providers(self):
        with (
            patch.object(runner, "compile_unit", side_effect=self.compile),
            patch.object(
                runner,
                "link_function",
                side_effect=lambda p, h, o, version, row, file: (bytes.fromhex(PROGRAMS[version]["hex"]), []),
            ),
        ):
            measured = compare.measure(self.project, self.host, self.file)
        self.assertTrue(measured.exact, measured.lines())
        self.assertEqual(measured.faults, {})
        self.assertEqual(self.syntax, [(version, 0) for version in self.versions])
        self.assertEqual(self.header.read_text(), self.old)
        self.assertEqual((self.private / HOME).read_text(), self.old)

    def test_normal_publication_without_old_c_owner_proves_the_complete_current_consumer(self):
        caller = "func_80257A14_de"
        source = (FIXTURE / "5B920-consumer.c").read_text()
        self.assertEqual(
            hashlib.sha256(source.encode()).hexdigest(),
            "e5218fc1f545a295d00297b7bed4f29f734f13d46602b5914cbe4d741373d0e1",
        )
        consumer = self.project.src / (caller + ".c")
        consumer.write_text(source)
        packets = json.loads((FIXTURE / "5B920-consumer-packets.json").read_text())["programs"]
        for version in self.versions:
            row = self.project.version(version)
            data = bytes.fromhex(PROGRAMS[version]["hex"])
            body = bytes.fromhex(packets[version]["hex"])
            end = 0x40 + len(data)
            row.baserom.write_bytes(row.baserom.read_bytes() + body)
            row.split.write_text(
                row.split.read_text().removesuffix(f"  - [{end}]\n")
                + f"  - name: caller\n    type: code\n    start: {end}\n    vram: 2150000000\n"
                f"    subsegments:\n      - [{end}, c, {caller}]\n  - [{end + len(body)}]\n"
            )
            row.symbols.write_text(row.symbols.read_text() + f"{caller} = 2150000000;\n")
        (self.project.root / "layout.toml").write_bytes(
            ownership.encoded(
                ownership.Map(
                    2,
                    (
                        ownership.Group("registry", "span_1000", "default", (FIVE,)),
                        ownership.Group("caller", "caller", "default", (caller,)),
                    ),
                )
            )
        )
        # Keep the authored consumer byte-for-byte. These live include slots use
        # only the retained CPP declaration closure, not invented field types.
        context = (FIXTURE / "5B920-consumer-context.h").read_text()
        primitive, registry = (FIXTURE / "5B920-context.h").read_text().split("typedef struct {", 1)
        (self.project.include[0] / "types.h").write_text(primitive)
        (self.private / "registry-context.h").write_text('#include "types.h"\ntypedef struct {' + registry)
        aliases = cdecl.declarations(primitive).typedefs
        for unit in declaration_evidence.units({self.root / "context.h": context}):
            if unit.types & aliases:
                context = context.replace(unit.text, "")
        for relative in (
            "common/types_1dc8418c21db.h",
            "common/types_8a8189af7b05.h",
            "span_1000/code_80256220.h",
            "span_1000/code_802591C0.h",
        ):
            path = self.project.include[0] / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include "consumer-context.h"\n')
        (self.project.include[0] / "consumer-context.h").write_text(
            "#ifndef CONSUMER_CONTEXT\n#define CONSUMER_CONTEXT\n" + context + "#endif\n"
        )
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Fixture"],
            ["git", "config", "user.email", "fixture@example.invalid"],
            ["git", "add", "."],
            ["git", "commit", "-qm", "Registry fixture"],
        ):
            subprocess.run(command, cwd=self.project.root, check=True, capture_output=True)
        self.consumer_work = []

        def native_consumer(view, host, unit, version, *, source):
            result = subprocess.run(
                [
                    self.compiler,
                    "-std=gnu89",
                    "-fsyntax-only",
                    *("-I" + str(root) for root in view.include),
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            self.consumer_work.append((version, result.returncode))
            if result.returncode:
                raise Held(named("compile.cc1", result.stderr, owner="fixture", stage="compile"))
            return bytes.fromhex(packets[version]["hex"])

        def linked(project, host, obj, version, *args):
            return bytes.fromhex(PROGRAMS[version]["hex"])

        entry = Attempt(
            "",
            FIVE,
            hashlib.sha256(self.source.encode()).hexdigest(),
            300,
            {version: {"exact": True} for version in self.versions},
            100,
            True,
            0,
            "ido-7.1",
        )
        events = []
        with (
            patch.object(
                pool,
                "run",
                side_effect=lambda h, fn, jobs, shared=None: [
                    fn(job) if shared is None else fn(shared, job) for job in jobs
                ],
            ),
            patch.object(runner, "compile_unit", side_effect=self.compile),
            patch.object(runner, "build_unit", side_effect=native_consumer),
            patch.object(runner, "place", return_value=[]),
            patch.object(runner, "link", side_effect=linked),
            patch.object(runner, "link_function", side_effect=lambda p, h, o, v, row, f: (linked(p, h, o, v), [])),
            patch.object(runner, "dependencies", return_value=set()),
            patch.object(land, "exact_attempt", return_value=entry),
            patch("unbake.work.compare_facts.attach"),
            patch("unbake.layout.structs_fold._prove_includers"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch.object(land.steps, "acknowledge_outputs"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            result = land.publish(self.project, self.host, [self.file], compare_first=True, on_commit=events.append)
        self.assertTrue(not result.failed, "\n".join(row["reason"] for row in result.failed.values()))
        self.assertEqual(result.landed, [FIVE])
        self.assertEqual(self.syntax, [(v, 0) for v in self.versions] * 2)
        self.assertEqual(self.consumer_work, [(v, 0) for v in self.versions])
        self.assertEqual(len(result.commits), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["proof"]["own_contract"]["previous_source_sha256"], None)
        self.assertEqual(consumer.read_text().removeprefix('#include "span_1000/code_8025A3EC.h"\n'), source)
