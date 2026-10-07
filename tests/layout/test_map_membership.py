"""Recorded RW publication payloads reconcile ownership without changing valid identities or doing a build."""

import copy
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.kit import TempCase
from unbake import build, buildfiles, steps
from unbake.config import Held
from unbake.layout import boundary_map, modules, split, split_apply
from unbake.layout import map as layout_map

FIXTURES = Path(__file__).parent / "fixtures/rw_membership"
NEW = "func_802C1B60_eu_x"
OLD_ENTRY = "func_80439F0C_us_rev1"
NEW_ENTRY = "func_80439F10_us_rev1"


class MembershipTests(TempCase):
    def setUp(self):
        super().setUp()
        self.processes = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process")))

    def project(self, filename):
        payload = json.loads((FIXTURES / filename).read_text())
        versions = {}
        for version, data in payload["versions"].items():
            directory = self.root / "versions" / version
            directory.mkdir(parents=True)
            yaml = directory / "RageWars.yaml"
            symbols = directory / "symbol_addrs.txt"
            yaml.write_text(data["yaml"])
            symbols.write_text(data["symbols"])
            versions[version] = SimpleNamespace(split=yaml, symbols=symbols, baserom=directory / "rom")
        project = SimpleNamespace(
            root=self.root,
            build=self.root / "build",
            src=self.root / "src",
            include=(self.root / "include",),
            names_from=payload["names_from"],
            versions=tuple(versions),
            version=versions.__getitem__,
        )
        self.write_layout(payload["layout"])
        return project, payload

    def write_layout(self, value):
        import toml

        (self.root / "layout.toml").write_text(toml.dumps(value))

    def facts(self, project, payload):
        # The reader boundary returns only the recorded function bytes, not a padded full ROM image.
        recorded = {}
        for version in project.versions:
            functions = [
                modules.Function(Path(f.path).name, "span_1000", f.start, f.end, f.address)
                for f in split.functions(project, version)
            ]
            recorded[version] = (
                functions,
                lambda f, v=version: [int(w, 16) for w in payload["versions"][v]["words"][f.name]],
                lambda address: False,
                None,
            )
        return patch.object(modules, "facts", side_effect=lambda p, v: recorded[v])

    def test_real_new_row_is_admitted_once_and_valid_inferred_group_is_preserved(self):
        project, payload = self.project("refill.json")
        with self.assertRaisesRegex(Held, f"layout.member.{NEW}: missing from map"):
            layout_map.load(project)
        original = layout_map.validate(
            payload["layout"], project.versions, {n: m for n, m in layout_map.catalog(project).items() if n != NEW}
        ).groups[0]
        target = project.root / "layout.toml"
        reads = []
        real_read = Path.read_bytes

        def read(path):
            if path == target:
                reads.append(path)
            return real_read(path)

        with (
            self.facts(project, payload) as facts,
            patch.object(modules, "infer", wraps=modules.infer) as infer,
            patch.object(layout_map, "catalog", wraps=layout_map.catalog) as catalog,
            patch.object(split, "functions", wraps=split.functions) as functions,
            patch.object(layout_map.atomic_files, "write", wraps=layout_map.atomic_files.write) as write,
            patch.object(Path, "read_bytes", read),
        ):
            self.assertTrue(layout_map.ensure(project))
            self.assertEqual(
                (catalog.call_count, functions.call_count, facts.call_count, infer.call_count), (1, 5, 5, 1)
            )
            self.assertEqual((write.call_count, len(reads)), (1, 1))
        result = layout_map.load(project)
        self.assertEqual(result.owners["__cmpdi2"], original)
        self.assertEqual(result.owners["__floatdisf"], original)
        self.assertEqual(
            tuple(sorted(result.owners)),
            ("__cmpdi2", "__floatdisf", "func_802C1878_de", "func_802C1908_de", NEW, "func_802C1BA8_eu"),
        )
        self.assertEqual((len(result.groups), sum(len(g.members) for g in result.groups)), (2, 6))
        self.assertEqual(result.owners[NEW].members, (NEW,))
        self.assertEqual(result.owners[NEW].only, {NEW: ("eu-x",)})
        member = layout_map.catalog(project)[NEW]
        self.assertEqual((member.address, member.segment, member.versions), (0x802C1B60, "span_1000", ("eu-x",)))
        with (
            patch.object(modules, "infer") as infer,
            patch.object(layout_map.atomic_files, "write") as write,
            patch.object(split, "functions", wraps=split.functions) as functions,
        ):
            self.assertFalse(layout_map.ensure(project))
            self.assertEqual((infer.call_count, write.call_count, functions.call_count), (0, 0, 5))
        self.processes.assert_not_called()

    def test_missing_new_row_changes_the_public_membership_currency_key(self):
        project, _ = self.project("refill.json")
        self.assertEqual(layout_map.stale(project), (NEW,))

    def test_authored_guards_refuse_before_inference_or_writes(self):
        project, payload = self.project("refill.json")
        base = payload["layout"]
        invalid = []
        unknown = copy.deepcopy(base)
        unknown["group"][0]["members"].append("func_absent")
        invalid.append((unknown, "layout.member.func_absent"))
        duplicate = copy.deepcopy(base)
        duplicate["group"].append(copy.deepcopy(duplicate["group"][0]))
        invalid.append((duplicate, "layout.member.func_802C1878_de"))
        order = copy.deepcopy(base)
        order["group"][0]["members"].reverse()
        invalid.append((order, "layout.group.code_80259B2C.members"))
        only = copy.deepcopy(base)
        only["group"][0]["only"] = {"func_absent": ["eu-x"]}
        invalid.append((only, "layout.only.func_absent"))
        schema = copy.deepcopy(base)
        schema["schema"] = 2
        invalid.append((schema, "layout.schema"))
        for value, key in invalid:
            with self.subTest(key):
                self.write_layout(value)
                before = (self.root / "layout.toml").read_bytes()
                with patch.object(modules, "infer") as infer, patch.object(layout_map.atomic_files, "write") as write:
                    with self.assertRaises(Held) as caught:
                        layout_map.ensure(project)
                    self.assertEqual(caught.exception.key, key)
                    self.assertEqual((infer.call_count, write.call_count), (0, 0))
                self.assertEqual((self.root / "layout.toml").read_bytes(), before)

    def test_inference_cannot_publish_incomplete_coverage(self):
        project, payload = self.project("refill.json")
        members = layout_map.catalog(project)
        incomplete = layout_map.validate(
            payload["layout"], project.versions, {n: m for n, m in members.items() if n != NEW}
        )
        with (
            patch.object(modules, "infer", return_value=incomplete) as infer,
            patch.object(layout_map.atomic_files, "write") as write,
        ):
            with self.assertRaisesRegex(Held, f"layout.member.{NEW}: missing from map"):
                layout_map.ensure(project)
            self.assertEqual((infer.call_count, write.call_count), (1, 0))

    def entry(self):
        project, payload = self.project("entry.json")
        change = boundary_map.Change(**payload["change"])
        rom = payload["rom"]
        data = bytes.fromhex(rom["hex"])
        self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (44, change.sha256))
        slices = []

        class Image:
            def __len__(self):
                return rom["end"]

            def __getitem__(self, item):
                slices.append((item.start, item.stop))
                if (item.start, item.stop) != (rom["start"], rom["end"]):
                    raise AssertionError("unrecorded ROM read")
                return data

        real_read = Path.read_bytes
        image = Image()
        rom_path = project.version(change.version).baserom
        reads = []

        def read(path):
            if path == rom_path:
                reads.append(path)
                return image
            return real_read(path)

        return project, change, slices, reads, patch.object(Path, "read_bytes", read)

    def test_byte_pinned_entry_plan_carries_membership_and_preserves_other_owners(self):
        project, change, slices, reads, image = self.entry()
        original = layout_map.load(project).groups[0]
        before = {
            p: p.read_bytes()
            for p in (
                project.root / "layout.toml",
                project.version(change.version).split,
                project.version(change.version).symbols,
            )
        }
        with image, patch.object(layout_map, "catalog", wraps=layout_map.catalog) as catalog:
            edits = boundary_map.plan(project, [change])
            self.assertEqual(catalog.call_count, 2)
        self.assertEqual((len(edits), len(reads), slices), (3, 1, [(0x1A7F0C, 0x1A7F38)]))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        layout = next(e for e in edits if e.path.name == "layout.toml")
        projected = layout_map.catalog(project, {e.path: e.after for e in edits})
        group = layout_map.validate(tomllib.loads(layout.after), project.versions, projected).groups[0]
        self.assertEqual(group.members, ("func_80439D00_de", NEW_ENTRY, "func_80439D58_de"))
        self.assertEqual(
            (group.name, group.segment, group.evidence, group.signals),
            (original.name, original.segment, original.evidence, original.signals),
        )
        self.assertEqual(group.only, {NEW_ENTRY: ("us-rev1",)})
        self.assertEqual(group.split, original.split)
        self.assertNotIn(OLD_ENTRY, projected)
        self.assertNotIn(OLD_ENTRY + "_prefix", projected)
        self.assertEqual(projected[NEW_ENTRY].address, 0x80439F10)
        self.assertEqual(
            {n: m for n, m in projected.items() if n != NEW_ENTRY},
            {n: m for n, m in layout_map.catalog(project).items() if n != OLD_ENTRY},
        )
        _, versions = split_apply._validated(project, edits)
        self.assertEqual(versions, list(project.versions))
        self.processes.assert_not_called()

    def test_canonical_layout_boundary_transaction_commits_or_reverts_every_file(self):
        project, change, _, _, image = self.entry()
        prepared = SimpleNamespace(assert_current=Mock())
        with image:
            edits = boundary_map.plan(project, [change])
        before = {e.path: e.path.read_bytes() for e in edits}
        for ok in (False, True):
            with self.subTest(ok):

                def proof(p, host, ok=ok):
                    owners = layout_map.load(p).owners
                    self.assertIn(NEW_ENTRY, owners)
                    self.assertNotIn(OLD_ENTRY, owners)
                    return build.Outcome(ok, True)

                with (
                    patch.object(steps, "prepare", return_value=prepared) as prepare,
                    patch.object(buildfiles, "generate", return_value=[]) as generate,
                    patch.object(buildfiles, "write", return_value=[]) as generate_write,
                    patch.object(build, "check", side_effect=proof) as check,
                    patch.object(split_apply.atomic_files, "write", wraps=split_apply.atomic_files.write) as write,
                ):
                    result = split_apply.apply(project, None, edits)
                    self.assertEqual(result.ok, ok)
                    self.assertEqual(
                        (prepare.call_count, generate.call_count, generate_write.call_count, check.call_count),
                        (1, 1, 1, 1),
                    )
                    self.assertEqual(write.call_count, 3 if ok else 5)
                if not ok:
                    self.assertEqual({p: p.read_bytes() for p in before}, before)
                else:
                    self.assertEqual(
                        {e.path: e.path.read_bytes() for e in edits}, {e.path: e.after.encode() for e in edits}
                    )
        self.processes.assert_not_called()
