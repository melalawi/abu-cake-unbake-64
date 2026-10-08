"""probe: the project's own recipe, the publish measurement, every artifact, and no project writes."""

import hashlib
import json
import re
import struct
from pathlib import Path
from unittest.mock import PropertyMock, patch

from tests.project_fixture import ProjectCase
from unbake import process, runner
from unbake.compilers import drivers
from unbake.config import Held
from unbake.work import attempts, compare, probe
from unbake.work.score import Measurement

GOOD = "int alpha(void) { return 1; }\n"


class ProbeTests(ProjectCase):
    def native(self, argv, work, phase, **kwargs):
        value = int(re.search(r"return (\d+)", (work / "alpha.i").read_text())[1])
        (work / "alpha.o").write_bytes(struct.pack(">III", 0x24020000 | value, 0x03E00008, 0))
        return ""

    def probe(self, source=GOOD, problems=(), **options):
        file = self.root / "try.c"
        file.write_text(source)

        def link(project, host, obj, version, row, path, capture_info=None):
            linked = obj.read_bytes()
            if capture_info is not None:
                capture_info.update(placed_object_sha256="p", linked_sha256=hashlib.sha256(linked).hexdigest())
            return linked, list(problems)

        with (
            patch.object(process, "run_tool", side_effect=self.native),
            patch.object(
                drivers,
                "run_preprocess",
                side_effect=lambda project, argv, phase, **kw: (
                    (project.root / argv[-1]).read_text()
                    if not Path(argv[-1]).is_absolute()
                    else Path(argv[-1]).read_text()
                ),
            ),
            # The strict native digest chain is mocked away: words equal to the target count as publish's exact.
            patch.object(Measurement, "exact", new_callable=PropertyMock, side_effect=lambda: True),
            patch.object(runner.toolchain, "verify", return_value={}),
            patch.object(runner, "link_function", side_effect=link),
            patch.object(probe, "_disassemble", side_effect=lambda host, binary, listing: listing.write_text("dis")),
            patch.object(probe, "_artifacts", return_value=["candidate.s"]) as artifacts,
        ):
            document = probe.run(self.project, self.host, "alpha", file, self.root / "out", **options)
        return document, artifacts

    def test_recipe_comes_from_project_config_and_only_named_overrides_change_it(self):
        unit = self.project.unit_path("alpha")
        base = drivers.resolved(self.project, "us", unit)
        self.assertEqual(base.phase("compile"), ("-O2", "-G0", "-mips2"))
        changed = probe.view(self.project, unit, flags=("-O1", "-DX"), omit=("-mips2",), compiler=None)
        resolved = drivers.resolved(changed, "us", unit)
        self.assertEqual(resolved.phase("compile"), ("-G0", "-O1"))
        self.assertIn("-DX", resolved.phase("preprocess"))
        self.assertEqual(self.project.recipe_for(unit), self.project.recipe_for(unit))
        with self.assertRaises(Held) as caught:
            probe.view(self.project, unit, flags=(), omit=(), compiler="nope")
        self.assertEqual(caught.exception.key, "probe.compiler")

    def test_dump_flags_stay_out_of_the_measured_recipe(self):
        document, artifacts = self.probe(flags=("-dgreg", "-O1"))
        self.assertEqual(artifacts.call_args.args[5], ("-dgreg",))
        self.assertNotIn("-dgreg", json.dumps(document["measurement"]["provenance"]["recipe"]))
        self.assertIn("-O1", document["measurement"]["provenance"]["recipe"]["options"]["compile"])

    def test_outputs_are_written_and_an_identical_candidate_is_exact(self):
        document, _ = self.probe()
        out = self.root / "out"
        for name in ("candidate.o", "linked.bin", "original.bin", "linked.dis", "original.dis", "result.json"):
            self.assertTrue((out / name).is_file(), name)
        self.assertEqual((out / "linked.bin").read_bytes(), (out / "original.bin").read_bytes())
        self.assertEqual(json.loads((out / "result.json").read_text()), json.loads(json.dumps(document)))
        self.assertTrue(document["exact"] and document["landable"])
        self.assertEqual(document["identical_words"], document["target_words"])
        self.assertEqual(document["version"], "us")
        self.assertTrue(str(self.project.compiler_for("alpha").cc).endswith(document["commands"]["compile_argv"][0]))
        self.assertIn("-O2", document["commands"]["compile_argv"])

    def test_probe_writes_nothing_to_the_project_or_its_ledger(self):
        def tree():
            return {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.project.root.rglob("*"))
                if p.is_file() and ".unbake/cache" not in str(p)
            }

        before = tree()
        with (
            patch.object(attempts, "RetryScope", side_effect=AssertionError("ledger scope")),
            patch.object(compare, "compare", side_effect=AssertionError("recorded compare")),
        ):
            self.probe()
        self.assertEqual(tree(), before)

    def test_probing_into_the_project_is_refused(self):
        file = self.root / "try.c"
        file.write_text(GOOD)
        with self.assertRaises(Held) as caught:
            probe.run(self.project, self.host, "alpha", file, self.project.root / "out")
        self.assertEqual(caught.exception.key, "probe.scope")

    def test_volatile_is_not_landable_and_not_exact(self):
        document, _ = self.probe("volatile int sink;\nint alpha(void) { return 1; }\n")
        self.assertFalse(document["landable"])
        self.assertFalse(document["exact"])
        self.assertEqual([row["rule"] for row in document["volatile"]], ["volatile-storage"])
        self.assertEqual(document["identical_words"], document["target_words"])

    def test_exact_needs_no_placement_problems(self):
        document, _ = self.probe(problems=("rodata: no proved resident address",))
        self.assertEqual(document["placement_problems"], ["rodata: no proved resident address"])
        self.assertFalse(document["exact"])
        self.assertTrue(document["landable"])
