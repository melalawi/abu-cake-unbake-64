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
from unbake import config, land, pool, runner
from unbake.config import Held
from unbake.layout import index
from unbake.process import named
from unbake.work import compare

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
        for root in (self.private, self.project.include[0]):
            for name, text in {
                HOME: self.old,
                "shared/func_8025B920_de_closed.h": (
                    '#include "registry-context.h"\n#include "span_1000/code_8025A3EC.h"\n'
                ),
                "registry-context.h": (FIXTURE / "5B920-context.h").read_text(),
            }.items():
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

    def test_public_replace_compare_keeps_proposed_headers_through_measure_and_stops_before_land(self):
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
            result = land.publish(self.project, self.host, [self.file], compare_first=True, replace_own_contract=True)
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

    def test_ordinary_comparison_keeps_the_conflicting_old_owner_and_all_native_faults(self):
        with patch.object(runner, "compile_unit", side_effect=self.compile):
            measured = compare.measure(self.project, self.host, self.file)
        self.assertFalse(measured.exact)
        self.assertEqual(set(measured.faults), set(self.versions))
        self.assertEqual(self.syntax, [(version, 1) for version in self.versions])
        self.assertTrue(all("conflicting types" in fault["cause"]["reason"] for fault in measured.faults.values()))
        self.assertEqual(self.header.read_text(), self.old)
        self.assertEqual((self.private / HOME).read_text(), self.old)
