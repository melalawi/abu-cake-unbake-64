"""Link input caches preserve content identity and isolate mutable transforms."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from tests.decomp.support import assemble
from unbake.project_tools.elf import Object
from unbake.project_tools.layout import place, place_object, transfer_private
from unbake.project_tools.link_inputs import Objects, Selectors, Spans, clone
from unbake.project_tools.literal_layout import arrange, replace


def parsed(path, *, data):
    obj = Object.__new__(Object)
    obj.path, obj.table, obj.data = Path(path), 0, bytearray(data)
    obj.sections = [[0, 1, 0, 0, 0, len(data), 0, 0, 1, 0]]
    obj.names = [".text"]
    obj.symbols = {1: [dict(table=1, index=0, name="alpha", value=0, size=0, info=0, section=0)]}
    return obj


class MetadataTests(unittest.TestCase):
    def test_persistent_identity_table(self):
        cases = (
            ("hit", b"original", b"parser", False, 0),
            ("same bytes different path", b"original", b"parser", True, 0),
            ("changed content same timestamp", b"modified", b"parser", False, 1),
            ("changed parser", b"original", b"new parser", False, 1),
        )
        for label, material, identity, rename, misses in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, cache = root / "alpha.o", root / "metadata"
                source.write_bytes(b"original")
                parser = Mock(side_effect=parsed)
                publish = Mock(side_effect=lambda path, data: path.write_bytes(data))
                read = Mock(side_effect=Path.read_bytes)
                with Objects(cache, parse=parser, read=read, publish=publish, parser_identity=b"parser") as load:
                    result = load(source)
                self.assertEqual(result.data, b"original")
                parser.assert_called_once_with(source, data=b"original")
                read.assert_called_once_with(source)
                publish.assert_called_once()
                stamp = source.stat().st_mtime_ns
                if rename:
                    source = root / "beta.o"
                source.write_bytes(material)
                os.utime(source, ns=(stamp, stamp))
                parser.reset_mock()
                publish.reset_mock()
                read.reset_mock()
                with Objects(cache, parse=parser, read=read, publish=publish, parser_identity=identity) as load:
                    result = load(source)
                self.assertEqual(result.path, source)
                self.assertEqual(result.data, material)
                self.assertEqual(parser.call_count, misses)
                self.assertEqual(publish.call_count, misses)
                self.assertEqual([call.args[0] for call in read.call_args_list], [cache, source])

    def test_memory_hits_and_clone_isolate_every_mutable_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "alpha.o"
            source.write_bytes(b"original")
            parser = Mock(side_effect=parsed)
            with Objects(root / "metadata", parse=parser, parser_identity=b"parser") as load:
                first = load(source)
                second = clone(first)
                for obj in (first, second):
                    obj.data[0] = 0
                    obj.names[0] = ".changed"
                    obj.sections[0][5] = 0
                    obj.symbols[1][0]["name"] = "changed"
                third = load(source)
                self.assertEqual(third.data, b"original")
                self.assertEqual(third.names, [".text"])
                self.assertEqual(third.sections[0][5], len(b"original"))
                self.assertEqual(third.symbols[1][0]["name"], "alpha")
                parser.assert_called_once()

    def test_invalid_cache_table(self):
        for fault in ("database", "metadata", "missing field"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, cache = root / "alpha.o", root / "metadata"
                source.write_bytes(b"original")
                with Objects(cache, parse=parsed, parser_identity=b"parser") as load:
                    load(source)
                if fault == "database":
                    cache.write_bytes(b"broken database")
                else:
                    with closing(sqlite3.connect(cache)) as database, database:
                        database.execute("UPDATE objects SET metadata = ?", ("{" if fault == "metadata" else "{}",))
                parser = Mock(side_effect=parsed)
                with Objects(cache, parse=parser, parser_identity=b"parser") as load:
                    self.assertEqual(load(source).data, b"original")
                parser.assert_called_once()

    def test_prefetch_table(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "alpha.o"
                error = FileNotFoundError("missing object")
                read = Mock(side_effect=error if failure else lambda path: b"original")
                parser = Mock(side_effect=parsed)
                pool = Mock()
                pool.__enter__ = Mock(return_value=pool)
                pool.__exit__ = Mock(return_value=False)
                pool.map.side_effect = lambda function, paths: map(function, paths)
                executor = Mock(return_value=pool)
                with Objects(root / "metadata", parse=parser, read=read, parser_identity=b"parser") as load:
                    load.prefetch([source, source], workers=3, executor=executor)
                    load.prefetch([source], workers=3, executor=executor)
                    read.assert_called_once_with(source)
                    if failure:
                        with self.assertRaises(FileNotFoundError) as raised:
                            load(source)
                        self.assertIs(raised.exception, error)
                        parser.assert_not_called()
                    else:
                        self.assertEqual(load(source).data, b"original")
                        parser.assert_called_once_with(source, data=b"original")
                    read.assert_called_once_with(source)
                self.assertEqual(executor.call_count, 2)
                executor.assert_called_with(max_workers=3)
                self.assertEqual(pool.__exit__.call_count, 2)

    def test_prefetch_batch_boundaries(self):
        for count in (0, 1, 64, 65, 129):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = [root / f"{number}.o" for number in range(count)]
                read = Mock(return_value=b"object")
                pool = Mock()
                pool.__enter__ = Mock(return_value=pool)
                pool.__exit__ = Mock(return_value=False)
                batches = []

                def execute(function, items, received=batches):
                    queued = list(items)
                    received.extend(queued)
                    return map(function, queued)

                pool.map.side_effect = execute
                with Objects(root / "metadata", read=read) as load:
                    load.prefetch([*paths, *paths], executor=Mock(return_value=pool))
                    self.assertEqual(len(load.pending), count)
                self.assertEqual(read.call_count, count)
                self.assertEqual(
                    [len(batch) for batch in batches], [min(64, count - start) for start in range(0, count, 64)]
                )
                pool.__exit__.assert_called_once()

    def test_failure_does_not_publish_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "alpha.o"
            source.write_bytes(b"original")
            publish = Mock()
            with (
                self.assertRaisesRegex(ValueError, "failed transform"),
                Objects(root / "metadata", parse=parsed, publish=publish) as load,
            ):
                load(source)
                raise ValueError("failed transform")
            publish.assert_not_called()


class SelectorTests(unittest.TestCase):
    def test_selector_table(self):
        for whitespace in ("", " ", "\n  "):
            with self.subTest(whitespace=whitespace):
                script = f"obj/src/a.o{whitespace}(.rodata); obj/src/ba.o(.rodata); obj/src/a.o(.text);"
                inputs = Selectors(script)
                self.assertTrue(inputs.contains("obj/src/a.o", ".rodata"))
                self.assertFalse(inputs.contains("obj/src/missing.o", ".rodata"))
                inputs.replace("obj/src/a.o", ".rodata", ".rdata")
                self.assertFalse(inputs.contains("obj/src/a.o", ".rodata"))
                self.assertTrue(inputs.contains("obj/src/a.o", ".rdata"))
                self.assertEqual(
                    inputs.apply(script), "obj/src/a.o(.rdata); obj/src/ba.o(.rodata); obj/src/a.o(.text);"
                )

    def test_pool_replacements_are_counted_and_consumed(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                script = "obj/asm/data/pool.o(.rodata);" * count
                inputs = Selectors(script)
                self.assertEqual(inputs.counts["obj/asm/data/pool.o", ".rodata"], count)
                if count == 1:
                    inputs.substitute("obj/asm/data/pool.o", ".rodata", "one(.pool); two(.pool)")
                    self.assertEqual(inputs.counts["obj/asm/data/pool.o", ".rodata"], 0)
                    self.assertEqual(inputs.apply(script), "one(.pool); two(.pool);")

    def test_place_object_uses_index_without_searching_script(self):
        from argparse import Namespace

        script = "obj/src/a.o(.text);obj/src/a.o(.rodata);obj/src/a.o(.rdata);"
        obj = parsed(Path("a.o"), data=b"text")
        load = Mock(return_value=obj)
        with patch("unbake.project_tools.layout.re.search", side_effect=AssertionError("script rescan")):
            self.assertEqual(
                place_object(
                    Namespace(build=Path("build")),
                    "obj/src/a.o",
                    script,
                    {"a": {}},
                    b"",
                    [],
                    [],
                    False,
                    load=load,
                    selectors=Selectors(script),
                ),
                script,
            )
        load.assert_called_once_with(Path("build/obj/src/a.o"))

    def test_global_and_unit_catalogue_table(self):
        from argparse import Namespace

        row = dict(path="rodata/pool", address=10, start=0, end=1)
        for global_catalogue in (False, True):
            with self.subTest(global_catalogue=global_catalogue), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "obj/src/a.o"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"object")
                (root / "script").write_text("obj/src/a.o(.text)")
                (root / "ranges").write_text(json.dumps({"a": {"rodata_slices": [row]}}))
                (root / "rom").write_bytes(b"image")
                if global_catalogue:
                    (root / "pool-providers.json").write_text(json.dumps([row]))
                args = Namespace(
                    build=root,
                    script=root / "script",
                    ranges=root / "ranges",
                    baserom=root / "rom",
                    recipe=None,
                    non_matching="0",
                    output=root / "output",
                )
                with patch("unbake.project_tools.layout.place_object", return_value="script") as transform:
                    place(args)
                self.assertNotIn("mapped_pools", transform.call_args.kwargs)
                inventory = transform.call_args.kwargs["inventory"]
                self.assertEqual(inventory is not None, global_catalogue)
                lookup = transform.call_args.kwargs["lookup"]
                if global_catalogue:
                    self.assertEqual(lookup.containing(10)[0]["path"], row["path"])
                else:
                    self.assertIsNone(lookup)

    def test_global_mapping_annotation_is_not_repeated(self):
        from argparse import Namespace

        class AlreadyMapped(list):
            def __iter__(self):
                raise AssertionError("global mappings scanned again")

        obj = parsed(Path("a.o"), data=b"text")
        obj.names = [".rdata"]
        inventory = [dict(path="rodata/pool", address=10, start=0, end=1)]
        lookup = Spans(inventory)
        with patch("unbake.project_tools.layout.transfer_private", return_value=[]) as transfer:
            place_object(
                Namespace(build=Path("build")),
                "obj/src/a.o",
                "",
                {"a": {}},
                b"",
                AlreadyMapped(),
                [],
                False,
                pools=[{"path": "rodata/pool"}],
                providers=[],
                load=Mock(return_value=obj),
                inventory=inventory,
                lookup=lookup,
            )
        transfer.assert_called_once()
        self.assertIs(transfer.call_args.args[3], inventory)
        self.assertIs(transfer.call_args.kwargs["lookup"], lookup)


class SpanTests(unittest.TestCase):
    def test_point_lookup_table(self):
        rows = [
            dict(address=10, start=0, end=5, name="first"),
            dict(address=0, start=0, end=20, name="wide"),
            dict(address=15, start=0, end=5, name="adjacent"),
            dict(address=10, start=0, end=5, name="duplicate"),
            dict(address=30, start=0, end=1, name="last"),
        ]
        for address in (-1, 0, 9, 10, 14, 15, 19, 20, 29, 30, 31):
            with self.subTest(address=address):
                expected = [
                    row for row in rows if row["address"] <= address < row["address"] + row["end"] - row["start"]
                ]
                self.assertEqual(Spans(rows).containing(address), expected)
        self.assertEqual(Spans([]).containing(0), [])


class TransformTests(unittest.TestCase):
    def test_replace_table(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                obj = parsed(Path("a.o"), data=bytes(40) + b"data")
                obj.sections[0][4:6] = [40, 4]
                before = bytes(obj.data)
                replace(obj, 0, b"next" if changed else b"data")
                self.assertEqual(obj.content(0), b"next" if changed else b"data")
                self.assertEqual(obj.data == before, not changed)

    def test_private_without_constants_never_reparses_or_writes(self):
        obj = parsed(Path("a.o"), data=b"text")
        with (
            patch("unbake.project_tools.layout.Object", side_effect=AssertionError("reparse")),
            patch("unbake.project_tools.layout.write") as publish,
        ):
            self.assertEqual(transfer_private(obj, {"start": 0, "end": 4, "address": 0}, bytes(4), []), [])
        publish.assert_not_called()

    def test_unchanged_pool_objects_and_remainders_skip_publication(self):
        from tests.project_tools.test_pool_ownership import PoolOwnershipTests
        from unbake.project_tools.pool_slices import link_pools

        for material in (b"hello\0", b"hello\0TAIL"):
            with self.subTest(material=material), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                row, image, script, providers, _ = PoolOwnershipTests().fixtures(root, material=material)
                first = link_pools(root, script, [row], providers, image)
                with patch("unbake.project_tools.pool_slices.write") as publish:
                    self.assertEqual(link_pools(root, script, [row], providers, image), first)
                publish.assert_not_called()

    def test_unchanged_literal_transform_skips_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj = Object(
                assemble(
                    root,
                    "alpha",
                    ".text\njr $ra\nnop\n.section .rdata\n"
                    ".globl unbake_rodata_80003000_4\nunbake_rodata_80003000_4: .word 0x12345678\n",
                )
            )
            raw = bytes.fromhex("12345678")
            read = Mock(return_value=raw)
            with patch("unbake.project_tools.literal_layout.write") as publish:
                for _ in range(2):
                    self.assertEqual(arrange(obj, ".rdata", {}, 0x80002000, read, emit_resident=True), 0x80003000)
                publish.assert_not_called()
            self.assertEqual(read.call_count, 4)
