"""Compiler relocations prove per-version table ownership independently of names."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.layout.test_rodata import Object, words
from unbake.layout.rodata_owners import Span, classify, proved_tables
from unbake.project.config import Held
from unbake.project_tools.rodata import table_addresses


class CompilerTableTests(unittest.TestCase):
    def object(self, section=".rdata", entries=(8, 12), references=1):
        pool = dict(name=section, value=0, section=1)
        local = dict(name="", value=0, section=0)
        return Object(
            section=section,
            data=words(*entries),
            code=words(0x3C010000, 0x24220000) * references,
            text_rels=[(at, kind, pool) for at in range(0, references * 8, 8) for at, kind in ((at, 5), (at + 4, 6))],
            data_rels=[(at * 4, 2, local) for at in range(len(entries))],
        )

    def test_version_section_and_bias_table(self):
        for version, section, bias in (
            ("de", ".rdata", 0),
            ("eu", ".rdata", 0x80000000),
            ("us", ".rodata", 0x80000000),
        ):
            with self.subTest(version=version, section=section):
                image = bytes(0x40) + words((0x80001008 - bias) & 0xFFFFFFFF, (0x8000100C - bias) & 0xFFFFFFFF)
                spans = [Span(0x80003000, 0x40, 0x48, bias, "resident")]
                target = words(0x3C018000, 0x24223000)
                obj = self.object(section)
                before = bytes(obj.data)
                tables = proved_tables(obj, "alpha", target, 0x80001000, image, spans)
                self.assertEqual(tables, [("alpha", 0x80003000, 0x80003008)])
                # A different executable row contains the destinations: ROM-only
                # heuristics cannot assign them to the compiler's owner.
                functions = [SimpleNamespace(address=0x80001000, start=0, end=16, path="beta")]
                result = classify(image, functions, spans, [], compiler_tables=tables)
                self.assertEqual(
                    [(r.kind, r.owners, r.end - r.address) for r in result], [("jump table", {"alpha"}, 8)]
                )
                self.assertTrue(result[0].safe_sole_candidate)
                self.assertEqual(bytes(obj.data), before)

    def test_proof_failure_table(self):
        cases = (
            ("missing reference", words(0x3C018000), [Span(0x80003000, 0x40, 0x48, 0, "resident")], "missing aligned"),
            (
                "changed instruction",
                words(0x3C028000, 0x24223000),
                [Span(0x80003000, 0x40, 0x48, 0, "resident")],
                "instruction differs",
            ),
            ("unmapped", words(0x3C018000, 0x24223000), [], "unique resident"),
            (
                "ambiguous",
                words(0x3C018000, 0x24223000),
                [Span(0x80003000, 0x40, 0x48, 0, "resident")] * 2,
                "unique resident",
            ),
            (
                "wrong bytes",
                words(0x3C018000, 0x24223000),
                [Span(0x80003000, 0x40, 0x48, 0, "resident")],
                "bytes disagree",
            ),
        )
        for label, target, spans, reason in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, reason):
                proved_tables(self.object(), "alpha", target, 0x80001000, bytes(0x48), spans)

    def test_repeated_references_require_agreement(self):
        obj = self.object(references=2)
        target = {0: 0x3C018000, 4: 0x24223000, 8: 0x3C018000, 12: 0x24223000}
        self.assertEqual(table_addresses(obj, ".rdata", target), {0: 0x80003000})
        target[12] += 4
        with self.assertRaisesRegex(ValueError, "conflicting"):
            table_addresses(obj, ".rdata", target)

    def test_authoritative_extent_is_exact_and_retains_other_owners(self):
        from unbake.layout.rodata_references import Reference

        image = bytes(0x40) + words(0x80001008, 0x8000100C, 0x80001000)
        spans = [Span(0x80003000, 0x40, 0x4C, 0, "resident")]
        functions = [SimpleNamespace(address=0x80001000, start=0, end=16, path="alpha")]
        refs = [Reference("beta", 0x80003000, "address", 0, 9, "explicit")]
        tables = [("alpha", 0x80003000, 0x80003008)]
        result = classify(image, functions, spans, refs, compiler_tables=tables)
        self.assertEqual([(r.end - r.address, r.kind) for r in result], [(8, "jump table"), (4, "other")])
        self.assertEqual(result[0].owners, {"alpha", "beta"})
        self.assertFalse(result[0].safe_sole_candidate)
        for invalid, reason in (
            (tables + [("beta", 0x80003000, 0x8000300C)], "conflicting"),
            (tables + [("beta", 0x80003004, 0x8000300C)], "overlapping"),
            ([("alpha", 0x80003001, 0x80003009)], "aligned"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(Held, reason):
                classify(image, functions, spans, [], compiler_tables=invalid)

    def test_scan_prefers_current_c_object_over_retained_assembly(self):
        from tests.layout.test_rodata_commands import RodataCommandTests
        from unbake.layout.rodata_owners import scan

        fixture = RodataCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        build = fixture.fixture.build_link("us")
        (build / "obj/src").mkdir()
        candidate = build / "obj/src/alpha.o"
        candidate.write_bytes((build / "obj/asm/alpha.o").read_bytes())
        with patch("unbake.layout.rodata_owners.proved_tables", return_value=[]) as proof:
            scan(fixture.project, "us")
        self.assertEqual(proof.call_args_list[0].args[0].path, candidate)
