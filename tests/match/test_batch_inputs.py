"""Batch input ownership and generation reachability, without external tools."""

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.decomp.needs import SymbolNeed
from unbake.match import batch, data_symbols, publication


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.symbols = self.root / "symbol_addrs.txt"
        self.symbols.write_text("D_800DE884_de = 0x800E28D4; // size:0x4 type:data\n")
        self.project = SimpleNamespace(names_from="de", version=lambda v: SimpleNamespace(symbols=self.symbols))
        self.need = SymbolNeed("us-rev1", "D_800E28D4", 0x800E28D4, 0, "data", "address", 0, "ROM")

    def test_real_duplicate_address_replaces_old_name_and_keeps_attributes(self):
        edits = data_symbols.resolve([self.need], self.project, object())
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].after, "D_800E28D4 = 0x800E28D4; // size:0x4 type:data\n")
        self.assertEqual(self.symbols.read_text(), edits[0].before)

    def test_existing_duplicate_is_coalesced(self):
        self.symbols.write_text(self.symbols.read_text() + "D_800E28D4 = 0x800E28D4; // absolute:True\n")
        edits = data_symbols.resolve([self.need], self.project, object())
        self.assertEqual(edits[0].after.count("= 0x800E28D4"), 1)
        self.assertIn("size:0x4 type:data", edits[0].after)

    def test_real_splat_diagnostic_names_only_introducing_sources(self):
        generation = self.root / "us-rev1.1"
        generation.mkdir()
        (generation / "build.log").write_text(
            "Duplicate symbol detected! D_800E28D4 clashes with D_800DE884_de defined at vram 0x800E28D4\n"
        )
        self.symbols.write_text("D_800E28D4 = 0x800E28D4;\n")
        owner = batch.Candidate("owner", Path("owner.c"), b"", "", ("us-rev1",), True, symbol_needs=[self.need])
        victim = batch.Candidate("victim", Path("victim.c"), b"", "", ("us-rev1",), True)
        faults = batch._input_culprits(self.project, [owner, victim], {"us-rev1": generation}, ["us-rev1"])
        self.assertEqual(set(faults), {"owner"})
        self.assertIn("symbol_addrs.txt:1", faults["owner"][0])

    def test_unattributed_version_error_publishes_remaining_sources(self):
        generation = self.root / "us-rev1.1"
        (generation / "obj/src").mkdir(parents=True)
        (generation / "obj/src/victim.o").touch()
        split = self.root / "split.yaml"
        split.write_text("original")
        self.project.version = lambda v: SimpleNamespace(symbols=self.symbols, split=split)
        owner = batch.Candidate("owner", Path("owner.c"), b"", "", ("us-rev1",), True, symbol_needs=[self.need])
        victim = batch.Candidate("victim", Path("victim.c"), b"", "", ("us-rev1",), True)
        (generation / "build.log").write_text(
            "Duplicate symbol detected! D_800E28D4 clashes with D_800DE884_de defined at vram 0x800E28D4\n"
        )
        receipts = []
        with (
            patch.object(batch.attribution, "diagnose", return_value={}),
            patch.object(batch.staging, "compare_failure", return_value="extract failed"),
            patch.object(batch.reporting, "record"),
            patch.object(batch, "_materialize") as materialize,
            patch.object(batch, "_relink", return_value={"us-rev1": SimpleNamespace(ok=True, sha1_line="OK")}),
            patch.object(batch, "_bisect") as bisect,
        ):
            passing, sha1 = batch._isolate(
                self.project,
                self.project,
                None,
                None,
                [owner, victim],
                {"us-rev1": generation},
                {"us-rev1": SimpleNamespace(ok=False)},
                receipts,
            )
        self.assertEqual(passing, [victim])
        self.assertEqual(sha1, {"us-rev1": "OK"})
        materialize.assert_called_once_with(self.project, None, [victim])
        bisect.assert_not_called()
        self.assertIn("data_symbols input", receipts[0])

    def test_materialize_restores_symbols_when_owner_is_removed(self):
        self.project.src = self.root / "src"
        self.project.src.mkdir()
        self.project.versions = ["us-rev1"]
        split = self.root / "split.yaml"
        split.write_text("")
        (self.root / "config.toml").write_text("")
        self.project.root = self.root
        self.project.version = lambda v: SimpleNamespace(symbols=self.symbols, split=split)
        base = SimpleNamespace(
            symbols={"us-rev1": self.symbols.read_text()},
            policy=object(),
            sources={},
            splits={"us-rev1": ""},
            config="",
            exclusions=None,
        )
        self.symbols.write_text("D_800E28D4 = 0x800E28D4;\n")
        with patch.object(batch, "_recipe"), patch.object(batch, "_rows", return_value=""):
            batch._materialize(self.project, base, [])
        self.assertEqual(self.symbols.read_text(), base.symbols["us-rev1"])


class CollectionTests(unittest.TestCase):
    def test_dead_reference_chains_are_collected_but_live_and_pinned_roots_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generations = [root / f"us.{i}" for i in range(6)]
            for generation in generations:
                (generation / "obj").mkdir(parents=True)
            # live 5 retains base 0; pinned 3 retains 2; dead 4 retains dead 1.
            for child, parent in ((5, 0), (3, 2), (4, 1)):
                (generations[child] / "obj/asm").symlink_to(generations[parent] / "obj")
            project = SimpleNamespace(build=root, versions=["us"])

            def flock(lock, flags):
                if Path(lock.name).parent == generations[3]:
                    raise BlockingIOError

            with (
                patch.object(publication.build, "lock", return_value=nullcontext()),
                patch.object(publication.build, "current_generation", return_value=generations[5]),
                patch.object(publication.fcntl, "flock", side_effect=flock),
            ):
                publication.collect(project)
            self.assertEqual({p.name for p in root.iterdir()}, {"us.0", "us.2", "us.3", "us.5"})
            self.assertTrue((generations[5] / "obj/asm").is_dir())


def local_project(directory, **kwargs):
    """Only input files; all object decoding and external tools are mocked."""
    root = directory / "project"
    root.mkdir()
    for name in ("src", "tools", "build"):
        (root / name).mkdir()
    split = root / "game.yaml"
    split.write_text(
        "name: fixture\nsegments:\n  - [0x0, header, header]\n"
        "  - name: main\n    type: code\n    start: 0x40\n    vram: 0x80001000\n"
        "    subalign: 4\n    subsegments:\n"
        "      - [0x40, asm, nonmatchings/alpha]\n      - [0x4C, asm, beta]\n"
        "      - [0x58, asm, gamma]\n  - [0x64]\n"
    )
    symbols = root / "symbol_addrs.txt"
    symbols.write_text("alpha = 0x80001000;\nbeta = 0x8000100C;\ngamma = 0x80001018;\n")
    baserom = root / "baserom.z64"
    baserom.write_bytes(
        bytes(0x40) + bytes.fromhex("2402000103e00008000000002402000203e00008000000002402000303e0000800000000")
    )
    version = SimpleNamespace(split=split, symbols=symbols, baserom=baserom)
    project = SimpleNamespace(
        root=root,
        src=root / "src",
        tools=root / "tools",
        build=root / "build",
        name="fixture",
        version=lambda v: version,
    )
    (project.build / "us.generation").mkdir()
    return project, None, None


class MockProofTests(unittest.TestCase):
    def _diagnose(self, directory, *, mixed):
        from unbake.match import attribution

        project, _, _ = local_project(directory)
        version = project.version("us")
        version.split.write_text(
            version.split.read_text()
            .replace("asm, nonmatchings/alpha", "c, alpha")
            .replace("asm, beta", "c, beta")
            .replace("asm, gamma", "c, gamma")
        )
        generation = project.build / "us.generation"
        (generation / "obj/src").mkdir(parents=True)
        for name in ("alpha", "beta", "gamma"):
            (generation / f"obj/src/{name}.o").touch()
        (generation / "build.log").write_text("src/alpha.c: invalid C\n" if mixed else "")
        (generation / "fixture.map").write_text(
            " .text 0x80001000 0x8 obj/src/alpha.o\n"
            " .text 0x80001008 0xc obj/src/beta.o\n"
            " .text 0x80001014 0xc obj/src/gamma.o\n"
        )

        def obj(path):
            name = path.stem
            code = {
                "alpha": "2402000103e00008",
                "beta": "2402000203e0000800000000",
                "gamma": "2402000303e0000800000000",
            }[name]
            if mixed and name == "gamma":
                code = "2402000403e0000800000000"
            symbols = [{"name": "missing", "section": 0, "info": 16}] if mixed and name == "beta" else []
            return SimpleNamespace(
                section=lambda s: 0 if s == ".text" else None,
                content=lambda i: bytes.fromhex(code),
                relocations=lambda i: [],
                symbols={0: symbols},
            )

        with patch.object(attribution, "Object", side_effect=obj):
            return attribution.diagnose(project, ["us"], {"us": generation}, {"alpha", "beta", "gamma"})

    def test_shift_origin_holds_only_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            faults = self._diagnose(Path(temporary), mixed=False)
        self.assertEqual(set(faults), {"alpha"})
        self.assertIn("shift origin", "; ".join(faults["alpha"]))

    def test_compile_binding_and_byte_failures_are_collected_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            faults = self._diagnose(Path(temporary), mixed=True)
        self.assertEqual(set(faults), {"alpha", "beta", "gamma"})
        self.assertIn("compile diagnostic", "; ".join(faults["alpha"]))
        self.assertIn("undefined reference to missing", "; ".join(faults["beta"]))
        self.assertIn("produced 24020004", "; ".join(faults["gamma"]))

    def test_port_fold_reuses_retained_graph_and_reverses_ownership(self):
        from tests.match import test_incremental

        # Exercise the graph test with a file-only fixture in place of its build fixture.
        with patch.object(test_incremental, "fixture", side_effect=local_project):
            case = test_incremental.RetargetTests()
            case.test_folded_entries_transfer_and_restore_without_extraction()
            case.test_changed_placement_refuses_without_writing_retained_files()
