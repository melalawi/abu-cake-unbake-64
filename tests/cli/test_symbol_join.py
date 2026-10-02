"""Installed CLI byte-pinned assertions, partial previews and named refusals."""

import hashlib
import json
import subprocess
import sys
import unittest

from tests.cli import test_symbol_rules as rules
from tests.cli.test_symbol_rules import LEFT, RIGHT, leaf


class SymbolJoinTests(unittest.TestCase):
    def setUp(self):
        self.fixture = rules.SymbolRuleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.project

    def command(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(self.fixture.launcher), "--project", str(self.project), *args],
            env=self.fixture.environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("Next:", result.stdout + result.stderr)
        return result.stdout + result.stderr

    def layout(self):
        return json.loads((self.project / "docs/setup/layout.json").read_bytes())

    def share(self, indices):
        layout = self.layout()
        for index in indices:
            first = layout["versions"]["us"]["functions"][index]
            other = layout["versions"]["us-rev1"]["functions"][index]
            old, new = other["name"], first["name"]
            other["name"] = new
            for path in (self.project / "versions/us-rev1").glob("*"):
                path.write_text(path.read_text().replace(old, new))
        (self.project / "docs/setup/layout.json").write_text(json.dumps(layout))

    def request(self, name, *positions):
        layout = self.layout()
        placements = []
        for version, index in positions:
            row = layout["versions"][version]["functions"][index]
            path = self.project / self.fixture.configuration["version"][version]["baserom"]
            image = path.read_bytes()
            placements.append(
                dict(
                    version=version,
                    start=row["start"],
                    end=row["end"],
                    body_sha256=hashlib.sha256(image[row["start"] : row["end"]]).hexdigest(),
                )
            )
        return dict(name=name, placements=placements, evidence={"source": "reviewed source correspondence"})

    def join(self, requests, *, apply=False, expected=0):
        path = self.fixture.directory / "joins.json"
        path.write_text(json.dumps(requests))
        args = ["split", "join", "--map", str(path)]
        if apply:
            args.append("--apply")
        return self.command(*args, expected=expected)

    def snapshot(self):
        return {
            path.relative_to(self.project): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file() and "build" not in path.relative_to(self.project).parts
        }

    def refusal(self, requests, reason):
        before = self.snapshot()
        output = self.join(requests, apply=True, expected=1)
        self.assertIn(reason, output)
        self.assertEqual(before, self.snapshot())

    def test_duplicate_version_including_existing_group_is_refused(self):
        self.fixture.inventory([LEFT, leaf(100), RIGHT], [LEFT, leaf(500), RIGHT])
        self.share([0])
        request = self.request("one", ("us", 1), ("us-rev1", 0))
        self.refusal([request], "split.join.duplicate_version")
        self.assertIn("one:", self.join([request], expected=1))

    def test_crossing_anchor_and_batch_crossing_refuse_every_change(self):
        self.fixture.inventory([LEFT, leaf(100), RIGHT, leaf(500), LEFT], [LEFT, leaf(200), RIGHT, leaf(700), LEFT])
        self.share([0, 2, 4])
        self.refusal([self.request("crossing", ("us", 1), ("us-rev1", 3))], "split.join.anchor_order")
        self.fixture.inventory([leaf(100), leaf(500)], [leaf(200), leaf(700)])
        self.refusal(
            [self.request("first", ("us", 0), ("us-rev1", 1)), self.request("second", ("us", 1), ("us-rev1", 0))],
            "split.join.anchor_order",
        )

    def test_proven_graph_contradiction_is_refused(self):
        target = 0x80001000
        caller_a = [0x0C000000 | (target >> 2 & 0x03FFFFFF), 0, 0x03E00008, 0]
        target += 4 * (len(LEFT) + len(caller_a))
        caller_b = [0x0C000000 | (target >> 2 & 0x03FFFFFF), 0, 0x03E00008, 0]
        self.fixture.inventory([LEFT, caller_a, RIGHT], [LEFT, caller_b, RIGHT])
        self.share([0, 2])
        self.refusal([self.request("wrong", ("us", 1), ("us-rev1", 1))], "split.join.graph_contradiction")

    def test_stale_bytes_boundary_and_overlapping_batch_refuse(self):
        self.fixture.inventory([LEFT, leaf(100), RIGHT], [LEFT, leaf(500), RIGHT])
        for field, value in (("end", 123), ("body_sha256", "0" * 64)):
            request = self.request("stale", ("us", 1), ("us-rev1", 1))
            request["placements"][0][field] = value
            self.refusal([request], "split.join.placement_stale")
        request = self.request("one", ("us", 1), ("us-rev1", 1))
        before = self.snapshot()
        output = self.join([request, dict(request, name="two")], expected=1)
        self.assertIn("split.join.overlap", output)
        self.assertIn("preview 1 passing joins; 1 refused requests", output)
        self.assertEqual(before, self.snapshot())
        self.assertIn("split join --map", output.split("Next: ", 1)[1])

    def test_unknown_transfers_are_recorded_without_claiming_graph_identity(self):
        a = [0x0080F809, 0, 0x03E00008, 0]
        b = [0x00A0F809, 0, 0x03E00008, 0]
        self.fixture.inventory([LEFT, a, RIGHT], [LEFT, b, RIGHT])
        self.share([0, 2])
        self.join([self.request("asserted", ("us", 1), ("us-rev1", 1))])
        report = json.loads(next((self.project / "build/setup").glob("join-*/proposal.json")).read_bytes())
        self.assertEqual(set(report["assertions"][0]["unknown_transfers"].values()), {"symbol-graph-indirect"})
        self.assertNotIn("symbol_assertions", self.layout())

    def test_refused_anchor_does_not_rename_or_anchor_the_passing_subset(self):
        self.fixture.inventory([LEFT, leaf(100), RIGHT, leaf(500)], [LEFT, leaf(200), RIGHT, leaf(700)])
        self.share([0, 2])
        good = self.request("good", ("us", 1), ("us-rev1", 1))
        wrong = self.request("wrong", ("us", 3), ("us-rev1", 0))
        output = self.join([good, wrong], expected=1)
        self.assertIn("preview 1 passing joins; 1 refused requests", output)
        report = json.loads(next((self.project / "build/setup").glob("join-*/proposal.json")).read_bytes())
        self.assertEqual([row["name"] for row in report["assertions"]], ["good"])
        self.assertNotIn("wrong", report["replacements"].values())

    def test_parallel_previews_have_separate_complete_receipts(self):
        self.fixture.inventory([LEFT, leaf(100), RIGHT], [LEFT, leaf(500), RIGHT])
        requests = [self.request("joined", ("us", 1), ("us-rev1", 1))]
        path = self.fixture.directory / "parallel joins.json"
        path.write_text(json.dumps(requests))
        command = [
            sys.executable,
            str(self.fixture.launcher),
            "--project",
            str(self.project),
            "split",
            "join",
            "--map",
            str(path),
        ]
        callers = [
            subprocess.Popen(
                command, env=self.fixture.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            for _ in range(2)
        ]
        for caller in callers:
            out, error = caller.communicate(timeout=90)
            self.assertEqual(caller.returncode, 0, out + error)
        artifacts = list((self.project / "build/setup").glob("join-*/proposal.json"))
        self.assertEqual(len(artifacts), 2)
        for artifact in artifacts:
            self.assertEqual(json.loads(artifact.read_bytes())["assertions"][0]["name"], "joined")
        self.assertFalse((self.project / "build/setup/join-proposal.json").exists())
