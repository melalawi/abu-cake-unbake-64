"""layout.toml is rendered from the current split: stale default members are planned again, and a layout refusal
names an edit rather than a command that reaches it again; a draft waits for the generated headers it includes."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake.cli import guidance
from unbake.config import Held
from unbake.layout import header_step, index
from unbake.layout import map as layout_map

MEMBERS = {
    name: layout_map.Member(name, "span_1000", address, ("de",))
    for name, address in (("func_80204030_de", 0x80204030), ("func_80204308_de", 0x80204308))
}


class StaleMemberTests(TempCase):
    def test_default_groups_drop_names_the_split_lost_and_others_keep_them(self) -> None:
        for evidence, members, expected in [
            ("default", ["func_8020402C_de", "func_80204308_de"], ["func_80204308_de"]),
            ("default", ["func_8020402C_de"], None),  # nothing left: the group goes, inference plans its rows
            ("proven", ["func_8020402C_de", "func_80204308_de"], ["func_8020402C_de", "func_80204308_de"]),
        ]:
            with self.subTest(evidence=evidence, members=members):
                group = {
                    "name": "code_80203B1C",
                    "evidence": evidence,
                    "members": list(members),
                    "only": {"func_8020402C_de": ["de"]},
                    "split": ["func_8020402C_de"],
                }
                value = {"group": [group]}
                layout_map._drop_stale_defaults(value, MEMBERS)
                if expected is None:
                    self.assertEqual(value["group"], [])
                    continue
                self.assertEqual(value["group"][0]["members"], expected)
                stale = "func_8020402C_de" in expected
                self.assertEqual("func_8020402C_de" in value["group"][0]["only"], stale)
                self.assertEqual(value["group"][0]["split"], ["func_8020402C_de"] if stale else [])

    def test_stale_lists_names_no_split_holds(self) -> None:
        (self.root / "layout.toml").write_text(
            'schema = 1\ncap = 32\n[[group]]\nname = "g"\nmembers = ["func_8020402C_de", "func_80204030_de"]\n'
        )
        with patch.object(layout_map, "catalog", return_value=MEMBERS):
            self.assertEqual(layout_map.stale(SimpleNamespace(root=self.root)), ("func_8020402C_de",))


class LayoutHoldNextTests(TempCase):
    def test_a_layout_refusal_names_an_edit_not_next(self) -> None:
        context = SimpleNamespace(command="cycle", cmd=lambda *words: "unbake " + " ".join(words))
        for reason, expected in [
            ("layout.member.func_8020402C_de: unknown member", "stop: fix the layout.toml group named above"),
            ("cycle.pick: nothing to work on", "unbake next"),
            (
                "compile.func_80294340_de: src/func_80294340_de.c: compile.cc1: cc1 exited 33: too many arguments",
                "stop: fix the C source named above (or the header the compiler names) so it compiles",
            ),
        ]:
            with self.subTest(reason):
                phase = reason.split(".", 1)[0]
                self.assertEqual(guidance.after(context, Held(phase, reason)), expected)  # type: ignore[arg-type]


class AbsentHeaderTests(TempCase):
    def test_generated_headers_a_draft_includes(self) -> None:
        include = self.root / "include"
        (include / "common").mkdir(parents=True)
        (self.root / "src").mkdir()
        (include / "common" / "types.h").write_text("typedef int s32;\n")
        (include / "audio.h").write_text('#include "common/data.h"\n')
        project = SimpleNamespace(include=(include,), src=self.root / "src")
        groups = (
            layout_map.Group("code_800C0880", "span_1000", "default", ("func_800C3EF0_us",)),
            layout_map.Group("code_800D0000", "span_1000", "default", ("func_800D0000_us",)),
        )
        generated = frozenset({include / "common" / "data.h", include / "common" / "types.h"})
        for label, present, functions, expected in [
            ("fresh tree", [], ("func_800C3EF0_us",), ["common/data.h", "span_1000/code_800C0880.h"]),
            ("other picks", [], ("func_800D0000_us",), ["common/data.h", "span_1000/code_800D0000.h"]),
            ("warm tree", ["common/data.h", "span_1000/code_800C0880.h"], ("func_800C3EF0_us",), []),
        ]:
            with (
                self.subTest(label),
                patch.object(layout_map, "load", return_value=SimpleNamespace(groups=groups)),
                patch.object(index, "listed", return_value=generated),
            ):
                for name in present:
                    (include / name).parent.mkdir(parents=True, exist_ok=True)
                    (include / name).write_text("\n")
                self.assertEqual(header_step.absent(project, functions), expected)  # type: ignore[arg-type]


class HistoryRenameTests(TempCase):
    def test_draft_history_moves_with_a_rename(self) -> None:
        from unbake.work import attempts

        work = self.root / "build" / "work"
        old, new = "func_8008EC78_us", "func_8008EC80_us"
        source = work / old
        (source / "us").mkdir(parents=True)
        (source / f"{old}.c").write_text(f"void {old}(void) {{}}\n")
        (source / "attempts.jsonl").write_text(json.dumps({"function": old}) + "\n")
        (source / "us" / f"{old}.o").write_bytes(b"\x7fELF")
        project = SimpleNamespace(work=work, root=self.root)
        carries = attempts.stage_renames(project, {old: new, "func_80000000_us": "func_80000008_us"})  # type: ignore[arg-type]
        self.assertTrue(source.is_dir() and not (work / new).exists())  # nothing live moves before the commit
        attempts.install(carries)
        self.assertFalse(source.exists())
        self.assertEqual((work / new / f"{new}.c").read_text(), f"void {new}(void) {{}}\n")
        self.assertEqual(json.loads((work / new / "attempts.jsonl").read_text())["function"], new)
        self.assertFalse((work / new / "us" / f"{new}.o").exists())  # objects are rebuilt, not carried
        self.assertEqual(sorted(p.name for p in work.iterdir()), [new])

    def test_a_taken_name_refuses_and_stages_nothing(self) -> None:
        from unbake.work import attempts

        work = self.root / "build" / "work"
        for name in ("func_A", "func_B", "func_C"):
            (work / name).mkdir(parents=True)
            (work / name / f"{name}.c").write_text("\n")
        project = SimpleNamespace(work=work, root=self.root)
        with self.assertRaises(Held) as caught:
            attempts.stage_renames(project, {"func_A": "func_Z", "func_B": "func_C"})  # type: ignore[arg-type]
        self.assertEqual(caught.exception.key, "attempts.rename")
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["func_A", "func_B", "func_C"])

    def test_the_committed_summary_follows_a_rename(self) -> None:
        from unbake.work import attempts

        project = SimpleNamespace(root=self.root)
        row = {"attempts": 1, "best": {"us": 50.0}, "bytes": 8, "exact": False, "minutes": 1.0}
        Path(self.root / "attempts.json").write_text(json.dumps({"functions": {"func_A": row}, "v": 1}))
        self.assertIsNone(attempts.renamed_summary(project, {"func_X": "func_Y"}))  # type: ignore[arg-type]
        renamed = json.loads(attempts.renamed_summary(project, {"func_A": "func_B"}) or b"")  # type: ignore[arg-type]
        self.assertEqual(renamed["functions"], {"func_B": row})


class HeaderCompileFailureTests(TempCase):
    def test_each_unit_reports_its_own_refusal_and_all_travel_together(self) -> None:
        import pickle

        from unbake import runner

        refusal = Held("compile", "compile.func_802450CC_de: src/func_802450CC_de.c: `D_800CB420_de' undeclared")
        for label, effect, expected in [
            ("compiles", None, None),
            ("refused", refusal, ("compile.func_802450CC_de", f"VERSION de: {refusal.reason}")),
        ]:
            with self.subTest(label), patch.object(runner, "compile_unit", side_effect=effect):
                job = (SimpleNamespace(), SimpleNamespace(), Path("u.c"), "de", "func_802450CC_de")
                self.assertEqual(header_step._compile(job), expected)  # type: ignore[arg-type]
        gathered = Held(
            "compile", "compile.headers: 2 unit compiles fail", failures=(("compile.a", "x"), ("compile.b", "y"))
        )
        self.assertEqual(pickle.loads(pickle.dumps(gathered)).failures, gathered.failures)
