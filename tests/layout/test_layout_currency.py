"""layout.toml is rendered from the current split: stale default members are planned again, and a layout refusal
names an edit rather than a command that reaches it again; a draft waits for the generated headers it includes."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from tests.kit import TempCase
from tests.project_fixture import ProjectCase
from unbake.cli import guidance
from unbake.config import Held
from unbake.layout import header_step
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


class RegroupTests(TempCase):
    """Every membership edit keeps `only` marks and `split` cuts on members, and refuses a bad edit itself."""

    VERSIONS = ("us", "de")
    BASE: ClassVar[dict[str, tuple[int, tuple[str, ...]]]] = {
        "a": (0x100, ("us", "de")),
        "b": (0x104, ("us",)),
        "c": (0x108, ("us", "de")),
        "x": (0x200, ("us", "de")),
    }

    def layout(self, only: dict[str, list[str]] | None = None) -> dict[str, object]:
        g = {"name": "g", "segment": "s", "evidence": "default", "members": ["a", "b", "c"]}
        g |= {"only": only if only is not None else {"b": ["us"]}, "split": ["b"]}
        return {
            "schema": 1,
            "cap": 32,
            "group": [g, {"name": "h", "segment": "s", "evidence": "default", "members": ["x"]}],
        }

    def regroup(self, catalog: dict[str, tuple[int, tuple[str, ...]]], **edit: object) -> layout_map.Map:
        members = {n: layout_map.Member(n, "s", address, held) for n, (address, held) in catalog.items()}
        return layout_map.regroup(self.layout(edit.pop("only", None)), members, self.VERSIONS, **edit)  # type: ignore[arg-type]

    def test_refusals(self) -> None:
        without_b = {k: v for k, v in self.BASE.items() if k != "b"}
        for label, catalog, edit, key in [
            ("replacement names an absent member", self.BASE, {"replacements": {"zz": ()}}, "layout.member.zz"),
            ("cut names an absent member", self.BASE, {"replacements": {}, "cuts": ["zz"]}, "layout.member.zz"),
            ("proven names an absent member", self.BASE, {"replacements": {}, "proven": ["zz"]}, "layout.member.zz"),
            ("rename onto another group's member", without_b, {"replacements": {"b": ("x",)}}, "layout.member.x"),
            ("drop while the row still exists", self.BASE, {"replacements": {"b": ()}}, "layout.member.b"),
            (
                "a mark that never named a member is not hidden",
                self.BASE,
                {"replacements": {"c": ()}, "only": {"b": ["us"], "q": ["us"]}},
                "layout.only.q",
            ),
        ]:
            with self.subTest(label), self.assertRaises(Held) as caught:
                self.regroup(dict(catalog), **edit)
            self.assertEqual(caught.exception.key, key)

    def test_edits(self) -> None:
        without_b = {k: v for k, v in self.BASE.items() if k != "b"}
        renamed = without_b | {"n": (0x104, ("us",))}
        moved = self.BASE | {"c": (0x0F0, ("us", "de"))}
        everywhere = self.BASE | {"b": (0x104, ("us", "de"))}
        for label, catalog, edit, members, only, cuts, evidence in [
            ("drop a version-only cut member", without_b, {"b": ()}, ("a", "c"), {}, (), "default"),
            (
                "rename a version-only member",
                renamed,
                {"b": ("n",)},
                ("a", "n", "c"),
                {"n": ("us",)},
                ("n",),
                "default",
            ),
            ("fold tail: now held everywhere", everywhere, {"b": ("b",)}, ("a", "b", "c"), {}, ("b",), "default"),
            ("move keeps address order", moved, {"c": ("c",)}, ("c", "a", "b"), {"b": ("us",)}, ("b",), "default"),
            ("untouched members keep marks", self.BASE, {}, ("a", "b", "c"), {"b": ("us",)}, ("b",), "default"),
        ]:
            with self.subTest(label):
                g = self.regroup(dict(catalog), replacements=edit).groups[0]
                self.assertEqual((g.members, g.only, g.split, g.evidence), (members, only, cuts, evidence))

    def test_merge_pass_edit(self) -> None:
        # The held RW pass: absorbed version-only members leave with their marks; refused runs become cuts.
        catalog = {k: v for k, v in self.BASE.items() if k != "b"}
        result = self.regroup(catalog, replacements={"b": ()}, cuts=["x"], proven=["a"])
        g, h = result.groups
        self.assertEqual((g.members, g.only, g.split, g.evidence), (("a", "c"), {}, (), "proven"))
        self.assertEqual((h.split, h.evidence), (("x",), "default"))
        empty = self.regroup({k: v for k, v in self.BASE.items() if k != "x"}, replacements={"x": ()})
        self.assertEqual([group.name for group in empty.groups], ["g"])


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


class HeaderCompileFailureTests(ProjectCase):
    def test_each_unit_reports_its_own_refusal_and_all_travel_together(self) -> None:
        import pickle

        from unbake import pool, runner

        sources = {self.project.src / f"{unit}.c": b"int missing;\n" for unit in ("alpha", "beta")}
        for source, data in sources.items():
            source.write_bytes(data)
        for version in self.versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace("asm, alpha]", "c, alpha]").replace("asm, beta]", "c, beta]"))
        job = (self.project, self.host, self.project.src / "alpha.c", "us", "alpha", False, False)
        with patch.object(runner, "compile_unit", return_value=nullcontext()) as compile_unit:
            self.assertIsNone(header_step._compile(job))
        compile_unit.assert_called_once_with(*job[:4], unit="alpha", non_matching=False)

        expected = []
        for unit in ("alpha", "beta"):
            for version in self.versions:
                reason = f"compile.{unit}: src/{unit}.c: `missing' undeclared"
                expected.append(
                    {
                        "key": f"compile.{unit}",
                        "reason": f"VERSION {version}: {reason}",
                        "fault": {"chain": [{"phase": "compile", "key": f"compile.{unit}", "reason": reason}]},
                    }
                )

        def refuse(view, host, file, version, *, unit, non_matching):
            raise Held("compile", f"compile.{unit}: src/{unit}.c: `missing' undeclared")

        with patch.object(runner, "compile_unit", side_effect=refuse):
            self.assertEqual(header_step._compile(job), expected[0])
            with (
                patch.object(pool, "run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]),
                self.assertRaises(Held) as caught,
            ):
                header_step.validate(self.project, self.host, sources)
        gathered = caught.exception
        self.assertEqual(gathered.key, "compile.headers")
        self.assertIn("4 unit compiles fail", gathered.reason)
        self.assertEqual(gathered.failures, tuple(expected))
        self.assertEqual(pickle.loads(pickle.dumps(gathered)).failures, tuple(expected))
