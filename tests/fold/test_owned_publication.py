"""Real complete native packets exercise the existing publication transaction."""

import hashlib
import json
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from tests.fold.test_owned_contract import BE, FIXTURE
from tests.project_fixture import ProjectCase
from unbake import land, pool, runner
from unbake.config import Held
from unbake.fold import self_prototype
from unbake.layout import map as ownership
from unbake.layout import split
from unbake.process import named as cause_named
from unbake.typemap import database, regeneration
from unbake.work import compare
from unbake.work.attempts import Attempt

CALLER = "func_802BD974_de"
PROGRAMS = json.loads((FIXTURE / "machine.json").read_text())["programs"]
RECORDS = json.loads((FIXTURE / "records.json").read_text())


class OwningPublicationTests(ProjectCase):
    versions = tuple(RECORDS["holders"])

    def setUp(self):
        super().setUp()
        self.header = self.project.include[-1] / "span_1000/owner.h"
        self.header.parent.mkdir(parents=True)
        self.before = (
            "#ifndef UNBAKE_SPAN_1000_OWNER_H\n#define UNBAKE_SPAN_1000_OWNER_H\n"
            + RECORDS["BE0C0_old_header"]
            + "#endif\n"
        )
        self.header.write_text(self.before)
        (self.project.include[-1] / "types.h").write_text(
            (FIXTURE / "sdk-context.h").read_text() + "\ntypedef int s32; typedef short s16;\n"
        )
        (self.project.include[-1] / "common").mkdir()
        (self.project.include[-1] / "common/unused.h").write_text('#include "../types.h"\n')
        self.previous = (FIXTURE / "old-owner.c").read_text().replace("span_1000/code_802BD1A8.h", "span_1000/owner.h")
        self.old_path = self.project.src / (BE + ".c")
        self.old_path.write_text(self.previous)
        self.consumer = self.project.src / (CALLER + ".c")
        self.original_consumer = (FIXTURE / (CALLER + ".c")).read_text()
        self.consumer.write_text(self.original_consumer)
        self.source = (FIXTURE / (BE + ".c")).read_text()
        self.file = self.root / (BE + ".c")
        self.file.write_text(self.source)
        (self.project.root / "layout.toml").write_bytes(
            ownership.encoded(
                ownership.Map(
                    2,
                    (
                        ownership.Group("owner", "span_1000", "default", (BE,)),
                        ownership.Group("caller", "span_caller", "default", (CALLER,)),
                        ownership.Group("gamma", "other", "default", ("gamma",)),
                    ),
                )
            )
        )
        for version in self.versions:
            callee = PROGRAMS[BE][version]
            caller = PROGRAMS[CALLER][version]
            first = bytes.fromhex(callee["hex"])
            second = bytes.fromhex(caller["hex"])
            rom = self.project.version(version).baserom
            rom.write_bytes(
                bytes.fromhex("80371240") + bytes(0x3C) + first + second + bytes.fromhex("2402000303e0000800000000")
            )
            last = 0x48 + len(second)
            self.project.version(version).split.write_text(
                "name: fixture\nsegments:\n  - [0x0, header, header]\n"
                f"  - name: span_1000\n    type: code\n    start: 0x40\n    vram: {callee['address']}\n"
                f"    subsegments:\n      - [0x40, c, {BE}]\n"
                f"  - name: span_caller\n    type: code\n    start: 0x48\n    vram: {caller['address']}\n"
                f"    subsegments:\n      - [0x48, c, {CALLER}]\n"
                f"  - name: other\n    type: code\n    start: {last}\n    vram: 2152726528\n"
                f"    subsegments:\n      - [{last}, asm, gamma]\n  - [{last + 12}]\n"
            )
            self.project.version(version).symbols.write_text(
                f"{BE} = {callee['address']};\n{CALLER} = {caller['address']};\ngamma = 0x80500000;\n"
            )
        self.attempt = Attempt(
            "",
            BE,
            hashlib.sha256(self.source.encode()).hexdigest(),
            8,
            {v: {"exact": True} for v in self.versions},
            100,
            True,
            0,
            "ido-7.1",
        )
        self.work = {"target": 0, "consumer": 0, "syntax": 0, "place": 0, "link": 0, "commit": 0}
        self.events = []
        self.mismatch = False
        self.change_owner = False
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Fixture"],
            ["git", "config", "user.email", "fixture@example.invalid"],
            ["git", "add", "."],
            ["git", "commit", "-qm", "Real packet fixture"],
        ):
            subprocess.run(command, cwd=self.project.root, check=True, capture_output=True)
        self.compiler = shutil.which("cc")
        self.assertIsNotNone(self.compiler)

    def git(self, project, *args, env=None):
        if args[:2] == ("show", "HEAD:" + str(self.old_path.relative_to(self.project.root))):
            return self.previous
        if args[:2] == ("rev-parse", "--git-path"):
            return str(self.project.root / ".git/index")
        if "commit" in args:
            self.work["commit"] += 1
        if args[:2] == ("rev-parse", "HEAD"):
            return "c0ffee\n"
        return ""

    def syntax(self, view, file, unit):
        header = next(
            root / "span_1000/owner.h"
            for root in (*view.work_include, *view.include)
            if (root / "span_1000/owner.h").is_file()
        )
        proposed = header.read_text()
        if unit == BE:
            text = proposed + self.source
        else:
            authored = file.read_text()
            prefix = '#include "span_1000/owner.h"\n'
            self.assertEqual(authored.removeprefix(prefix), self.consumer.read_text())
            cpp = (FIXTURE / "family-caller.c").read_text()
            if f"extern void {BE}(void);" in authored:
                cpp = f"extern void {BE}(void);\n" + cpp
            text = proposed + cpp
        input_file = self.root / f"syntax-{self.work['syntax']}.c"
        input_file.write_text(text)
        self.work["syntax"] += 1
        result = subprocess.run(
            [self.compiler, "-std=gnu89", "-fsyntax-only", str(input_file)], text=True, capture_output=True
        )
        if result.returncode:
            raise Held(
                cause_named("compile.cc1", "compile.cc1: " + result.stderr[-900:], owner="compile", stage="compile")
            )

    @contextmanager
    def compile(self, view, host, file, version, *, unit, **options):
        self.syntax(view, file, unit)
        self.work["target"] += 1
        yield self.root / "fixture.o"

    def build(self, view, host, unit, version, *, source):
        self.syntax(view, source, unit)
        self.work["consumer"] += 1
        data = bytes.fromhex(PROGRAMS[CALLER][version]["hex"])
        if self.change_owner:
            self.old_path.write_text(self.previous + "\n/* changed after proof */\n")
        return data[:-1] + bytes([data[-1] ^ 1]) if self.mismatch else data

    @contextmanager
    def boundaries(self):
        def place(*args, **kwargs):
            self.work["place"] += 1
            return []

        def link(project, host, placed, version, row, directory, file):
            self.work["link"] += 1
            return bytes.fromhex(PROGRAMS[BE][version]["hex"])

        with (
            patch.object(
                pool,
                "run",
                side_effect=lambda host, fn, jobs, shared=None: [
                    fn(job) if shared is None else fn(shared, job) for job in jobs
                ],
            ),
            patch.object(land, "exact_attempt", return_value=self.attempt),
            patch.object(land, "_git", side_effect=self.git),
            patch.object(runner, "compile_unit", side_effect=self.compile),
            patch.object(runner, "build_unit", side_effect=self.build),
            patch.object(runner, "place", side_effect=place),
            patch.object(runner, "link", side_effect=link),
            patch.object(runner, "dependencies", return_value=set()),
            patch("unbake.layout.structs_fold._prove_includers"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch.object(land.steps, "acknowledge_outputs"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            yield

    def assert_authority_unchanged(self):
        self.assertEqual(self.header.read_text(), self.before)
        self.assertEqual(self.old_path.read_text(), self.previous)
        self.assertEqual(self.consumer.read_text(), self.original_consumer)
        self.assertEqual(self.work["commit"], 0)

    def test_public_complete_owning_transition_proves_exact_target_and_consumer_work_before_one_commit(self):
        writes = []
        original = land.atomic_files.write

        def write(path, data):
            if path in {self.header, self.old_path, self.consumer, self.project.root / "config.toml"}:
                writes.append(path)
            return original(path, data)

        with self.boundaries(), patch.object(land.atomic_files, "write", side_effect=write):
            commit = land.land(self.project, self.host, self.file, on_commit=self.events.append)
        self.assertEqual(commit, "c0ffee")
        self.assertEqual(self.work, {"target": 5, "consumer": 5, "syntax": 10, "place": 5, "link": 5, "commit": 1})
        self.assertEqual(writes, [self.old_path, self.consumer, self.header, self.project.root / "config.toml"])
        self.assertEqual(
            self.consumer.read_text().removeprefix('#include "span_1000/owner.h"\n'), self.original_consumer
        )
        self.assertEqual(
            "\n".join(line for line in self.old_path.read_text().splitlines() if not line.startswith("#include"))
            + "\n",
            self.source,
        )
        self.assertIn("unsigned char unused_code", self.header.read_text())
        proof = self.events[0]["proof"]["own_contract"]
        self.assertEqual(proof["previous_source_sha256"], hashlib.sha256(self.previous.encode()).hexdigest())
        self.assertEqual(proof["proposed_source_sha256"], self.attempt.sha256)
        self.assertEqual(
            proof["previous_provider_sha256"]["span_1000/owner.h"], hashlib.sha256(self.before.encode()).hexdigest()
        )
        for version in self.versions:
            self.assertEqual(next(row.kind for row in split.functions(self.project, version) if row.name == BE), "c")

    def test_incomplete_forged_or_wrong_receipts_refuse_before_native_or_shared_write(self):
        original = self.attempt
        for bad in (
            None,
            replace(original, exact=False),
            replace(original, sha256="0" * 64),
            replace(original, function="caller"),
            replace(original, versions={}),
            replace(original, versions={**original.versions, self.versions[0]: {"exact": False}}),
            replace(original, versions={**original.versions, self.versions[0]: {"exact": True, "fault": "cc1"}}),
        ):
            self.attempt = bad
            with self.boundaries(), self.assertRaisesRegex(Held, "complete exact comparison"):
                land.land(self.project, self.host, self.file)
            self.assert_authority_unchanged()
            self.assertEqual(self.work["target"], 0)

    def test_current_conflicting_extern_and_native_consumer_change_refuse_without_mutation(self):
        for conflicting in (False, True):
            with self.subTest(conflicting=conflicting):
                self.work = dict.fromkeys(self.work, 0)
                self.mismatch = not conflicting
                self.original_consumer = (FIXTURE / (CALLER + ".c")).read_text()
                if conflicting:
                    self.original_consumer = self.original_consumer.replace(
                        f"extern void {BE}(void *, unsigned char);", f"extern void {BE}(void);"
                    )
                self.consumer.write_text(self.original_consumer)
                with self.boundaries(), self.assertRaises(Held):
                    land.land(self.project, self.host, self.file)
                self.assert_authority_unchanged()
                self.assertEqual(self.work["target"], 5)

    def test_existing_owner_mutation_after_proof_cannot_commit_replacement(self):
        self.change_owner = True
        with self.boundaries(), self.assertRaisesRegex(Held, "owning source changed since proof"):
            land.land(self.project, self.host, self.file)
        self.assertEqual(self.header.read_text(), self.before)
        self.assertEqual(self.consumer.read_text(), self.original_consumer)
        self.assertEqual(self.old_path.read_text(), self.previous + "\n/* changed after proof */\n")
        self.assertEqual(self.work["commit"], 0)

    @contextmanager
    def comparison_boundaries(self):
        def linked(project, host, obj, version, row, file):
            return bytes.fromhex(PROGRAMS[BE][version]["hex"]), []

        with patch.object(runner, "link_function", side_effect=linked), patch("unbake.work.compare_facts.attach"):
            yield

    def test_normal_comparison_is_private_and_creates_only_a_real_comparison_receipt(self):
        with self.boundaries(), self.comparison_boundaries():
            measured = compare.compare(self.project, self.host, self.file)
        self.assertTrue(measured.exact, measured.lines())
        self.assertEqual(self.work["target"], 5)
        self.assert_authority_unchanged()
        self.assertEqual(self.file.read_text(), self.source)

    def test_normal_public_compare_then_publish_requires_target_and_current_consumers(self):
        with (
            self.boundaries(),
            self.comparison_boundaries(),
            patch.object(self_prototype, "plan", wraps=self_prototype.plan) as plans,
            patch.object(self_prototype, "previous_source", wraps=self_prototype.previous_source) as source_reads,
        ):
            result = land.publish(
                self.project, self.host, [self.file], compare_first=True, on_commit=self.events.append
            )
        self.assertEqual(result.failed, {})
        self.assertEqual(result.landed, [BE])
        self.assertEqual(self.work, {"target": 10, "consumer": 5, "syntax": 15, "place": 5, "link": 5, "commit": 1})
        self.assertEqual(len(self.events), 1)
        self.assertEqual((plans.call_count, source_reads.call_count), (2, 2))

    def test_existing_render_retires_old_stored_copy_by_installed_name_without_database_or_schema_patch(self):
        self.header.write_text(
            self_prototype.plan(self.project, {self.header: self.before}, self.source, BE, self.versions).edits[0].after
        )
        self.old_path.write_text(self.source)
        self.consumer.unlink()
        value = {kind: {} for kind in ("structs", "functions", "globals", "arrays")}
        value["functions"][BE] = {
            "state": "known",
            "prototype": f"void {BE}(void *, unsigned char);",
            "provenance": [{"kind": "proven", "function": BE}],
        }
        value["published_declarations"] = {".published_old.h": RECORDS["BE0C0_old_header"]}
        value["published_homes"] = {".published_old.h": ["span_1000/owner.h"]}
        with self.boundaries():
            session = regeneration.Session(self.project, self.host)
            database._render(self.project, value, None, session)
        self.assertNotIn(".published_old.h", value["published_declarations"])
        self.assertFalse((self.project.build / "types.sqlite").exists())

    def test_unsupported_transport_remains_a_default_public_refusal_without_native_or_writes(self):
        for signature in (
            "float value",
            "double value",
            "long long value",
            "int a, int b, int c, int d, int e",
            "int a, ...",
        ):
            self.file.write_text(f"void {BE}({signature}) {{}}\n")
            with self.boundaries():
                result = land.publish(self.project, self.host, [self.file], compare_first=True)
            self.assertIn(BE, result.failed)
            self.assertEqual(result.failed[BE]["key"], "land.own_contract")
            self.assert_authority_unchanged()
            self.assertEqual(self.work["target"], 0)

    def test_changed_contract_without_exact_receipt_cannot_be_retained_as_fuzzy(self):
        self.old_path.unlink()
        for version in self.versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace("c, " + BE, "asm, " + BE))
        with self.boundaries():
            result = land.publish(self.project, self.host, [self.file], fuzzy=True)
        self.assertIn(BE, result.failed)
        self.assertEqual(result.failed[BE]["key"], "land.own_contract")
        self.assertEqual(self.header.read_text(), self.before)
        self.assertEqual(self.work["target"], 0)
