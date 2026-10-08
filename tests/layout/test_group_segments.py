"""Groups cut at code segment boundaries; data-only sources are not code consumers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from tests.layout.test_split import ProjectFixture
from unbake.config import Held, Project
from unbake.layout import map as layout_map
from unbake.layout import split

EVERYWHERE = ("de", "us")


def member(name: str, segment: str, address: int) -> layout_map.Member:
    return layout_map.Member(name, segment, address, EVERYWHERE)


def group(**fields: Any) -> dict[str, Any]:
    return {"name": "code_80426310", "segment": "span_166000", "evidence": "proven", "signals": ["cap"], **fields}


MEMBERS = {
    "func_80426310_de": member("func_80426310_de", "span_166000", 0x80426310),
    "func_80428FA0_de": member("func_80428FA0_de", "span_166000", 0x80428FA0),
    "func_8043C688_de": member("func_8043C688_de", "span_16E000", 0x8043C688),
    "func_8043C998_de": member("func_8043C998_de", "span_16E000", 0x8043C998),
}


class SplitCrossingTests(unittest.TestCase):
    """Real shape: code_80426310 declared span_166000 while 12 members live in span_16E000."""

    def cut(self, **fields: Any) -> list[dict[str, Any]]:
        value = {"group": [group(members=list(MEMBERS), **fields)]}
        layout_map._split_crossing(value, MEMBERS)
        return cast(list[dict[str, Any]], value["group"])

    def test_one_group_per_contiguous_segment_run_with_each_runs_segment(self) -> None:
        first, second = self.cut()
        self.assertEqual((first["name"], first["segment"]), ("code_80426310", "span_166000"))
        self.assertEqual(first["members"], ["func_80426310_de", "func_80428FA0_de"])
        self.assertEqual((second["name"], second["segment"]), ("code_80426310_16E000", "span_16E000"))
        self.assertEqual(second["members"], ["func_8043C688_de", "func_8043C998_de"])

    def test_evidence_signals_marks_and_cuts_follow_their_members(self) -> None:
        first, second = self.cut(only={"func_8043C688_de": ["de"]}, split=["func_8043C998_de"])
        self.assertEqual((first["only"], first["split"]), ({}, []))
        self.assertEqual((second["only"], second["split"]), ({"func_8043C688_de": ["de"]}, ["func_8043C998_de"]))
        self.assertEqual((second["evidence"], second["signals"]), ("proven", ["cap"]))

    def test_split_groups_validate_as_a_complete_map(self) -> None:
        value = {"schema": 1, "cap": 4, "group": self.cut()}
        parsed = layout_map.validate(value, EVERYWHERE, MEMBERS)
        self.assertEqual([g.segment for g in parsed.groups], ["span_166000", "span_16E000"])

    def test_single_segment_group_is_untouched(self) -> None:
        names = ["func_80426310_de", "func_80428FA0_de"]
        value = {"group": [group(members=names)]}
        before = repr(value)
        layout_map._split_crossing(value, MEMBERS)
        self.assertEqual(repr(value), before)

    def test_name_clash_in_a_segment_gets_a_distinct_label(self) -> None:
        value = {
            "group": [
                group(members=list(MEMBERS)),
                group(name="code_80426310_16E000", segment="span_16E000", members=["x"]),
            ]
        }
        members = {**MEMBERS, "x": member("x", "span_16E000", 0x80500000)}
        layout_map._split_crossing(value, members)
        self.assertEqual(len({(g["segment"], g["name"]) for g in value["group"]}), len(value["group"]))


class DataOnlySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = ProjectFixture(Path(self.directory.name), ("one", "two", "three", "four", "five"))
        self.project = cast(Project, self.fixture)

    def test_data_unit_stem_holds_no_code_version_and_is_not_an_error(self) -> None:
        self.assertEqual(split.code_versions(self.project, "pool"), ())
        self.assertEqual(split.code_versions(self.project, "beta"), split.holding_versions(self.project, "beta"))

    def test_naming_an_absent_function_still_refuses_by_name(self) -> None:
        with self.assertRaisesRegex(Held, "pool: split row missing in every VERSION"):
            split.holding_versions(self.project, "pool")


if __name__ == "__main__":
    unittest.main()
