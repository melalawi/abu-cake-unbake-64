"""Submit reuse preserves proof boundaries and avoids project-sized scans."""

import hashlib
import json
import os
import pickle
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.layout.header_context import Headers
from unbake.match import batch, batch_fold, forked, incremental, relink, reporting, source_views, staging
from unbake.project.config import Held
from unbake.project_tools.rodata import insert_fragment


class FoldWarmupTests(unittest.TestCase):
    def test_warmup_selects_versions_without_repeating_source_parses(self):
        cases = [
            (b"int a(void) {return 0;}", []),
            (b"struct Old {int value;};", ["us"]),
            (b"struct Old __attribute__((packed)) {int value;};", ["us"]),
            (b"#if VERSION_US\nstruct Old {int value;};\n#endif", ["eu", "us"]),
        ]
        for text, versions in cases:
            with self.subTest(text=text):
                candidate = SimpleNamespace(function="a", content=text, versions=("us", "eu"))
                with (
                    patch.object(source_views, "parsers", side_effect=AssertionError("serial candidate parse")),
                    patch.object(source_views, "typed_context", return_value="typedef int Word;") as preprocess,
                    patch.object(batch_fold.type_rewrite, "_context") as parsed,
                ):
                    batch_fold._warm_contexts(None, SimpleNamespace(cache_root=Path("cache")), None, [candidate])
                self.assertEqual([call.args[-1] for call in preprocess.call_args_list], versions)
                self.assertEqual(parsed.call_count, int(bool(versions)))
        candidate = SimpleNamespace(function="a", content=b"struct Old {int value;};", versions=("us",))
        with patch.object(source_views, "typed_context", side_effect=Held("fold", "bad source")):
            batch_fold._warm_contexts(None, None, None, [candidate])


class VersionPreparationTests(unittest.TestCase):
    def test_rejected_or_advanced_version_keeps_dirty_inventory_and_snapshot_separate(self):
        for advanced in (False, True):
            with self.subTest(advanced=advanced), tempfile.TemporaryDirectory() as directory:
                generation = Path(directory)
                (generation / "game.ld").write_text("raw")
                (generation / "game.link.ld").write_text("placed")
                split_file = generation / "split.yaml"
                split_file.write_text("after")
                project = SimpleNamespace(
                    name="game", version=lambda v, split_file=split_file: SimpleNamespace(split=split_file)
                )
                source = generation / "new.c"
                with (
                    patch.object(incremental, "advance", return_value=advanced),
                    patch.object(incremental, "changed_sources", return_value=[source]) as dirty,
                ):
                    result = incremental._prepare_version(
                        (project, project, {"us": generation}, {"us": "before"}), "us"
                    )
                self.assertEqual(result, [source] if advanced else None)
                self.assertEqual(dirty.call_count, int(advanced))
                snapshot = generation / "retained-layout.json"
                self.assertEqual(snapshot.exists(), advanced)
                if advanced:
                    self.assertEqual(
                        json.loads(snapshot.read_text()), {"raw": "raw", "placed": "placed", "sources": ["new"]}
                    )


class RetainedPlacementTests(unittest.TestCase):
    def test_places_only_dirty_units_and_reuses_unchanged_selectors_and_overlays(self):
        for changed in ([], ["new"], ["old"]):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                generation = Path(directory)
                raw = "SECTIONS {\n obj/src/old.o(.rodata);\n /DISCARD/ : { *(.unused) }\n}\n"
                overlay = "  .resident_80001000 0x80001000 (NOLOAD) : SUBALIGN(1) { obj/src/old.o(.rdata) }"
                placed = insert_fragment(raw.replace("(.rodata)", "(.rdata)"), overlay)
                (generation / "retained-layout.json").write_text(
                    json.dumps({"raw": raw, "placed": placed, "sources": changed})
                )
                (generation / "game.ld").write_text(raw.replace(" /DISCARD/", " obj/src/new.o(.text);\n /DISCARD/"))
                (generation / "unit-ranges.json").write_text("{}")
                image = generation / "rom"
                image.write_bytes(b"rom")
                project = SimpleNamespace(name="game", version=lambda v, image=image: SimpleNamespace(baserom=image))
                with (
                    patch.object(relink.makefile, "description", return_value={}),
                    patch(
                        "unbake.project_tools.layout.place_object", side_effect=lambda args, name, script, *rest: script
                    ) as place,
                ):
                    self.assertTrue(relink.place_changed(project, "us", generation))
                self.assertEqual(
                    [call.args[1] for call in place.call_args_list], [f"obj/src/{name}.o" for name in changed]
                )
                text = (generation / "game.link.ld").read_text()
                self.assertEqual(".resident_80001000" in text, "old" not in changed)
                self.assertIn("obj/src/old.o(.rodata)" if "old" in changed else "obj/src/old.o(.rdata)", text)
                self.assertEqual(
                    (generation / "game.link.flags").read_bytes(), b"" if "old" in changed else b"--no-check-sections"
                )

    def test_uncertified_transformations_and_failed_placement_use_full_proof(self):
        for failure in ("missing", "other transformation", "object fault"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                generation = Path(directory)
                raw = "SECTIONS {\n obj/src/a.o(.text);\n /DISCARD/ : { *(.unused) }\n}\n"
                (generation / "game.ld").write_text(raw)
                (generation / "game.link.ld").write_text("previous result")
                (generation / "unit-ranges.json").write_text("{}")
                image = generation / "rom"
                image.write_bytes(b"rom")
                if failure != "missing":
                    (generation / "retained-layout.json").write_text(
                        json.dumps(
                            {
                                "raw": raw,
                                "placed": raw if failure == "object fault" else raw + "unexpected",
                                "sources": ["a"],
                            }
                        )
                    )
                project = SimpleNamespace(name="game", version=lambda v, image=image: SimpleNamespace(baserom=image))
                with (
                    patch.object(relink.makefile, "description", return_value={}),
                    patch("unbake.project_tools.layout.place_object", side_effect=ValueError("bad object")) as place,
                ):
                    self.assertFalse(relink.place_changed(project, "us", generation))
                self.assertEqual(place.call_count, int(failure == "object fault"))
                self.assertEqual((generation / "game.link.ld").read_text(), "previous result")


def plus(shared, item):
    reporting.learn(f"receipt {item}")
    return shared + item


class FakePool:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.closed = False
        self.maps = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def shutdown(self, *, cancel_futures):
        self.closed = True

    def submit(self, fn, item):
        self.maps += 1
        future = Future()
        try:
            future.set_result(fn(item))
        except BaseException as error:
            future.set_exception(error)
        return future


class BatchPoolTests(unittest.TestCase):
    def test_one_pool_for_multiple_snapshots_and_nested_sessions(self):
        pools = []

        def create(**kwargs):
            pool = FakePool(**kwargs)
            pools.append(pool)
            return pool

        with (
            patch.object(forked, "ProcessPoolExecutor", side_effect=create),
            patch.object(forked.threading, "active_count", return_value=1),
            patch.object(forked, "_snapshot", None),
            patch.object(forked.pickle, "load", wraps=pickle.load) as load,
        ):
            with forked.session(8, 3), forked.session(8, 3):
                first = list(forked.ordered(plus, 10, [1, 2, 3], 8))
                with patch.object(forked.threading, "active_count", return_value=2):
                    second = list(forked.ordered(plus, 20, [1, 2], 8))
                self.assertEqual(load.call_count, 2)
                self.assertEqual(len(pools), 1)
                self.assertEqual(pools[0].options["max_workers"], forked.MAX_WORKERS)
                snapshot = Path(forked._snapshot[0])
                self.assertFalse(snapshot.exists())
            self.assertFalse(snapshot.exists())
            self.assertIsNone(forked._pool.get())
        self.assertTrue(pools[0].closed)
        self.assertEqual(first, [([f"receipt {i}"], 10 + i) for i in [1, 2, 3]])
        self.assertEqual(second, [([f"receipt {i}"], 20 + i) for i in [1, 2]])

    def test_serial_boundaries_and_exception_close_the_pool(self):
        for cores, jobs, threads in [(1, 3, 1), (8, 1, 1), (8, 3, 2)]:
            with (
                self.subTest(cores=cores, jobs=jobs, threads=threads),
                patch.object(forked, "ProcessPoolExecutor", side_effect=AssertionError("unexpected pool")),
                patch.object(forked.threading, "active_count", return_value=threads),
                forked.session(cores, jobs),
            ):
                list(forked.ordered(plus, 1, list(range(jobs)), cores))
        pool = FakePool()
        with (
            patch.object(forked, "ProcessPoolExecutor", return_value=pool),
            patch.object(forked.threading, "active_count", return_value=1),
            self.assertRaisesRegex(ValueError, "stop"),
            forked.session(2, 2),
        ):
            raise ValueError("stop")
        self.assertTrue(pool.closed)
        self.assertIsNone(forked._pool.get())

    def test_header_snapshot_is_picklable_and_does_not_track_parent_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "types.h"
            headers = Headers({header: "typedef int Word; struct Box { Word value; };"}, root=header.parent)
            captured = pickle.loads(pickle.dumps(headers))
            headers.apply([])
            headers.texts[header] = "typedef float Word;"
            self.assertIn("typedef int Word", captured.texts[header])
            self.assertEqual(captured.records[0].size, 4)


class IndexedStagingTests(unittest.TestCase):
    def test_retention_only_visits_indexed_objects_and_keeps_receipts_private(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "old", Path(directory) / "new"
            new.mkdir()
            for name in ["src/a", "asm/nested/b", "assets/data.bin", "src/unindexed"]:
                path = old / "obj" / (name + ".o")
                path.parent.mkdir(parents=True, exist_ok=True)
                for suffix in [".o", ".d", ".inputs.json", ".built"]:
                    path.with_suffix(suffix).write_bytes(suffix.encode())
            (old / ".split.mk").write_text(
                "C_OBJECTS := $(BUILD)/obj/src/a.o\n"
                "ASM_OBJECTS := $(BUILD)/obj/asm/nested/b.o\n"
                "ASSET_OBJECTS := $(BUILD)/obj/assets/data.bin.o\n"
            )
            (old / "retained-layout.json").write_text("old certificate")
            with patch.object(Path, "rglob", side_effect=AssertionError("build-tree scan")):
                staging.retain(old, new)
                staging.publication_stamps(None, {"us": new})
            self.assertFalse((new / "retained-layout.json").exists())
            self.assertFalse((new / "obj/src/unindexed.o").exists())
            obj = Path("obj/src/a.o")
            self.assertEqual((old / obj).stat().st_ino, (new / obj).stat().st_ino)
            self.assertNotEqual(
                (old / obj).with_suffix(".built").stat().st_ino, (new / obj).with_suffix(".built").stat().st_ino
            )
            (new / obj).with_suffix(".built").write_bytes(b"new receipt")
            self.assertEqual((old / obj).with_suffix(".built").read_bytes(), b".built")
            from unbake.project_tools.atomic import write

            write(new / obj, b"changed object")
            self.assertEqual((old / obj).read_bytes(), b".o")

    def test_inventory_refuses_escape_and_missing_graph_never_walks(self):
        for graph, expected in [(None, ()), ("C_OBJECTS := $(BUILD)/obj/../outside.o\n", "held")]:
            with self.subTest(graph=graph), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if graph is not None:
                    (root / ".split.mk").write_text(graph)
                with patch.object(Path, "rglob", side_effect=AssertionError("scan")):
                    if expected == "held":
                        with self.assertRaisesRegex(Held, "outside generation"):
                            staging.object_paths(root)
                    else:
                        self.assertEqual(staging.object_paths(root), expected)

    def test_stale_receipt_chunks_use_index_and_keep_assets_and_new_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "tools"
            tools.mkdir()
            recipe = tools / "build.json"
            symbols = root / "symbols.txt"
            recipe.write_text(
                json.dumps(
                    {
                        "units": {},
                        "default_compiler": "fixture",
                        "assembly_compiler": None,
                        "compilers": {"fixture": {"kind": "ido", "cc": "tools/fixture/cc"}},
                    }
                )
            )
            symbols.write_text("symbols")
            driver = tools / "compile/drivers/codegen.py.sha256"
            driver.parent.mkdir(parents=True)
            driver.write_text("scoped generator")
            assembly = tools / "compile" / root.name / "assembly.json"
            assembly.parent.mkdir(parents=True)
            assembly.write_text("{}")
            os.utime(driver, ns=(100, 100))
            os.utime(assembly, ns=(200, 200))
            names = [("src/old", 99), ("src/new", 150), ("asm/old", 150), ("asm/new", 250), ("assets/a.bin", 1)]
            (root / ".split.mk").write_text(
                "C_OBJECTS := " + " ".join(f"$(BUILD)/obj/{name}.o" for name, _ in names) + "\n"
            )
            for name, stamp in names:
                path = root / "obj" / (name + ".built")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                os.utime(path, ns=(stamp, stamp))
            with patch.object(Path, "rglob", side_effect=AssertionError("scan")):
                staging.chunk_stale_sources(root, tools, symbols)
            self.assertEqual(
                [name for name, _ in names if (root / "obj" / (name + ".built")).exists()],
                ["src/new", "asm/new", "assets/a.bin"],
            )

    def test_linked_input_copy_prunes_outputs_and_atomic_edits_leave_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            for name in ["build/huge/file", "work/private/file", "drafts/file", "asm/us/a.s", "src/a.c"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"input")
            project = SimpleNamespace(
                root=root, build=root / "build", work=root / "work", drafts=root / "drafts", asm=root / "asm"
            )
            destination = root.parent / "staged"
            staging.copy_tree(project, root, destination, assembly=False, linked=True)
            self.assertEqual(list(destination.iterdir()), [destination / "src"])
            self.assertEqual((root / "src/a.c").stat().st_ino, (destination / "src/a.c").stat().st_ino)
            from unbake.layout.split_apply import write

            write(destination / "src/a.c", "edited")
            self.assertEqual((root / "src/a.c").read_bytes(), b"input")


class ReusedObjectAdmissionTests(unittest.TestCase):
    def test_exact_recipe_dependency_source_and_receipt_identity_required(self):
        cases = [
            "valid",
            "recipe",
            "header",
            "source",
            "missing source digest",
            "receipt",
            "old receipt",
            "cache helper",
            "cache wrapper",
            "object",
            "compiler",
        ]
        for fault in cases:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tools = root / "tools"
                tools.mkdir()
                recipe = tools / "build.json"
                recipe.write_text('{"flags": ["-O2"]}')
                driver = tools / "compile.py"
                driver.write_text("driver")
                source = root / "src/a.c"
                source.parent.mkdir()
                source.write_text("int a(void) {return 1;}")
                header = root / "include/a.h"
                header.parent.mkdir()
                header.write_text("typedef int Word;")
                obj = root / "generation/obj/src/a.o"
                obj.parent.mkdir(parents=True)
                obj.write_bytes(b"object")
                receipt = obj.with_suffix(".built")
                receipt.touch()
                evidence = {
                    str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in [source, header]
                }
                os.utime(recipe, ns=(100, 100))
                os.utime(driver, ns=(100, 100))
                os.utime(receipt, ns=(200, 200))
                if fault == "cache helper":
                    cache = tools / "cache.py"
                    cache.write_text("cache implementation changed")
                    os.utime(cache, ns=(300, 300))
                if fault == "cache wrapper":
                    driver.write_text("new cache wrapper with unchanged compiler inputs")
                    os.utime(driver, ns=(300, 300))
                if fault in ["header", "source"]:
                    (header if fault == "header" else source).write_text("edited")
                if fault == "missing source digest":
                    evidence.pop("src/a.c")
                if fault in ["receipt", "object"]:
                    (receipt if fault == "receipt" else obj).unlink()
                if fault == "old receipt":
                    os.utime(receipt, ns=(50, 50))
                obj.with_suffix(".inputs.json").write_text(json.dumps(evidence))
                project = SimpleNamespace(
                    root=root, tools=tools, src=source.parent, compiler_for=lambda p: SimpleNamespace(id="cc")
                )
                with (
                    patch.object(
                        incremental.makefile,
                        "description",
                        return_value={"flags": ["-O0" if fault == "recipe" else "-O2"]},
                    ),
                    patch("unbake.project.toolchain.specification", return_value="spec"),
                    patch(
                        "unbake.project.toolchain.verify",
                        side_effect=Held("setup", "compiler changed") if fault == "compiler" else None,
                    ),
                ):
                    if fault == "compiler":
                        with self.assertRaisesRegex(Held, "compiler changed"):
                            incremental.reusable_sources(project, {"us": root / "generation"}, {"a": ("us",)})
                    else:
                        self.assertEqual(
                            incremental.reusable_sources(project, {"us": root / "generation"}, {"a": ("us",)}),
                            {"a"} if fault in {"valid", "cache helper", "cache wrapper"} else set(),
                        )

    def test_checked_sources_preserve_bytes_and_still_enter_normal_proof(self):
        source = b'#include "types.h"\nint a(void) {return 1;}\n'
        candidate = batch.Candidate(
            "a", Path("a.c"), source, "digest", ("us",), True, republication=True, compiled=True
        )
        with (
            patch.object(batch.Headers, "read", side_effect=AssertionError("header parse")),
            patch.object(batch.build, "compile_versions", side_effect=AssertionError("compile")),
        ):
            self.assertEqual(batch._fold(None, None, [candidate], []), [candidate])
            self.assertEqual(candidate.final.encode(), source)
            self.assertEqual(batch._type_preflight(None, None, [candidate], []), [candidate])
            self.assertEqual(batch._data_symbols(None, None, [candidate], []), [candidate])
            batch._materialize(None, None, [candidate])


@dataclass
class ContextProject:
    root: Path
    include: tuple[Path, ...]
    default_compiler: str
    compilers: dict
    macros: dict
    overlay_roots: tuple[Path, ...] = ()

    def version(self, name):
        return SimpleNamespace(macros=self.macros[name])


class SharedHeaderContextTests(unittest.TestCase):
    def test_effective_headers_flags_and_macros_invalidate_one_materialized_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / "include/types.h"
            headers = Headers({header: "typedef int Word;"}, root=root)
            macros = {"us": ("US",), "eu": ("EU",)}
            project = ContextProject(root, (header.parent,), "cc", {"cc": SimpleNamespace(cflags=("-O2",))}, macros)
            policy = SimpleNamespace(cpp=Path("cpp"), cppflags=("-P",))

            def preprocess(local, policy, version):
                return (local.include[0] / "types.h").read_text() + str(macros[version])

            with patch("unbake.typemap.declarations.headers", side_effect=preprocess) as cpp:
                first = source_views.typed_context(project, policy, headers, "us")
                self.assertEqual(source_views.typed_context(project, policy, headers, "us"), first)
                self.assertEqual(cpp.call_count, 1)
                self.assertNotEqual(source_views.typed_context(project, policy, headers, "eu"), first)
                headers.texts[header] = "typedef float Word;"
                self.assertNotEqual(source_views.typed_context(project, policy, headers, "us"), first)
                policy.cppflags = ("-P", "-DNEW")
                source_views.typed_context(project, policy, headers, "us")
                self.assertEqual(cpp.call_count, 4)

    def test_batch_relinks_each_version_once_and_never_trusts_unextracted_placement(self):
        for reusable in [False, True]:
            with self.subTest(reusable=reusable), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                split_file = root / "split"
                split_file.write_text("current")
                project = SimpleNamespace(version=lambda v, split_file=split_file: SimpleNamespace(split=split_file))
                policy = SimpleNamespace(cores=2, setup_version_jobs=2)
                with (
                    patch.object(relink, "revert_rows", return_value=reusable),
                    patch.object(relink, "place_changed", return_value=True) as placement,
                    patch.object(relink, "prove", return_value="proved") as proof,
                ):
                    result = batch._relink(
                        project, policy, {"us": root / "us", "eu": root / "eu"}, {"us": "previous", "eu": "previous"}
                    )
                self.assertEqual(result, {"us": "proved", "eu": "proved"})
                self.assertEqual(proof.call_count, 2)
                self.assertEqual(placement.call_count, 2 if reusable else 0)
                for call in proof.call_args_list:
                    self.assertEqual(call.kwargs, {"extracted": reusable, "placed": reusable})


class BorrowedProofTests(unittest.TestCase):
    def test_unchanged_placement_preserves_any_certified_script_without_replaying_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, placed = "original graph", "previous complete placed graph"
            (root / "game.ld").write_text(raw)
            (root / "game.link.flags").write_bytes(b"old flags")
            (root / "retained-layout.json").write_text(json.dumps({"raw": raw, "placed": placed, "sources": []}))
            self.assertTrue(relink.place_changed(SimpleNamespace(name="game"), "us", root))
            self.assertEqual((root / "game.link.ld").read_text(), placed)
            self.assertEqual((root / "game.link.flags").read_bytes(), b"old flags")

    def test_borrowed_objects_are_detached_before_any_changed_dependency_is_compiled(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                old, proof = root / "old", root / "proof"
                old.mkdir()
                proof.mkdir()
                obj = old / "obj/src/a.o"
                obj.parent.mkdir(parents=True)
                obj.write_bytes(b"certified object")
                (old / ".split.mk").write_text("C_OBJECTS := $(BUILD)/obj/src/a.o\n")
                split_file = root / "split"
                split_file.write_text("split")
                project = SimpleNamespace(
                    name="game", version=lambda v, split_file=split_file: SimpleNamespace(split=split_file)
                )
                staging.retain(old, proof, borrowed=True)
                self.assertTrue((proof / "obj").is_symlink())
                with (
                    patch.object(incremental, "advance", return_value=True),
                    patch.object(incremental, "changed_sources", return_value=[root / "a.c"] if dirty else []),
                ):
                    incremental._prepare_version((project, project, {"us": proof}, {"us": "before"}), "us")
                self.assertEqual((proof / "obj").is_symlink(), not dirty)
                self.assertEqual(obj.read_bytes(), b"certified object")
