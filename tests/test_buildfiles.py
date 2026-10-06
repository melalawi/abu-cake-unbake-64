"""Generated build data: raw slices between published C rows, and the unit table."""

import re
from dataclasses import replace
from unittest import mock

from tests import project_fixture
from tests.project_fixture import ProjectCase
from unbake import buildfiles, config

ROWS = {"alpha": 0x40, "beta": 0x4C, "gamma": 0x58}
ROM_END = 0x64


class BuildfileTests(ProjectCase):
    versions = ("us", "eu")

    def publish(self, name: str, versions: tuple[str, ...]) -> None:
        (self.project.src / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
        for version in versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace(f"asm, {name}]", f"c, {name}]"))

    def slices(self, version: str) -> set[tuple[int, int]]:
        text = buildfiles.slices_mk(config.load(self.project.root), version)
        return {(int(a), int(b)) for a, b in re.findall(rf"^{version}\.S\.\w+ := (\d+) (\d+)$", text, re.M)}

    def test_slices_cover_exactly_the_bytes_no_unit_covers(self) -> None:
        cases = [
            ("all asm", (), {(0, ROM_END)}),
            ("middle unit splits the run", ("beta",), {(0, 0x4C), (0x58, 0xC)}),
            ("adjacent units leave one run", ("alpha", "beta"), {(0, 0x40), (0x58, 0xC)}),
            ("last unit shortens the run", ("gamma",), {(0, 0x58)}),
            ("all C leaves only the header", ("alpha", "beta", "gamma"), {(0, 0x40)}),
        ]
        for label, published, expected in cases:
            with self.subTest(label):
                self.setUp()
                for name in published:
                    self.publish(name, self.versions)
                self.assertEqual(self.slices("us"), expected)

    def test_version_only_row_changes_only_that_version(self) -> None:
        self.publish("beta", ("us",))
        self.assertEqual(self.slices("us"), {(0, 0x4C), (0x58, 0xC)})
        self.assertEqual(self.slices("eu"), {(0, ROM_END)})

    def test_units_table_lists_only_exceptions(self) -> None:
        for name in ("alpha", "beta"):
            self.publish(name, self.versions)
        plain = buildfiles.units_mk(config.load(self.project.root))
        self.assertIn("alpha.key", plain)
        self.assertIn("PREPROCESS_FLAGS", plain)
        flagged = buildfiles.units_mk(replace(config.load(self.project.root), unit_flags={"alpha": ("-O1",)}))
        self.assertIn("build/%/src/alpha.key build/%/units/alpha.bin: UNIT_CODEGEN := -O1", flagged)
        self.assertNotIn("beta.bin: UNIT_CODEGEN", flagged)

    def test_makefile_builds_every_version_in_one_graph(self) -> None:
        for versions in (("us",), ("us", "eu", "eu-x", "de", "us-rev1")):
            with self.subTest(versions=len(versions)):
                project, host = project_fixture.make(self.root / str(len(versions)), versions=versions)
                text = buildfiles.makefile(project, host)
                self.assertIn(f"VERSIONS := {' '.join(versions)}\n", text)
                self.assertIn("$(foreach v,$(VERSIONS),$(eval $(call VERSION_RULES,$v)))\n", text)
                self.assertIn("include $(foreach v,$(VERSIONS),versions/$v/slices.mk)\n", text)
                self.assertIn("check: verify $(ROMS)\n", text)
                self.assertNotIn("$(MAKE)", text)
                self.assertNotIn("OBJCOPY", text)
                for version in versions:
                    slices = buildfiles.slices_mk(project, version)
                    self.assertIn(f"\n{version}.BASEROM := roms/baserom.{version}.z64\n", slices)
                    names = [line.split(" := ")[0] for line in slices.splitlines() if " := " in line]
                    self.assertTrue(names and all(name.startswith(f"{version}.") for name in names), names)

    def test_unit_recipe_compiles_once_per_content_key(self) -> None:
        text = buildfiles.makefile(self.project, self.host)
        key = text[text.index("UNIT_KEY =") : text.index("UNIT_BIN =")]
        key = " ".join(key.replace("\\\n", " ").split())
        for step in (
            "$(PREPROCESS_$(KIND)) &&",
            "| sha1sum - $(@D)/$(*F).i)",
            "[ -f build/cas/$$1$$3.o ] ||",
            "(cd $(@D) && $(COMPILE_$(KIND))) && mv -f $(@D)/$(*F).o build/cas/$$1$$3.o",
            "printf '%s\\n' $$1$$3 > $(@D)/$(*F).key",
        ):
            self.assertIn(step, key)
        self.assertIn("'$(TOOLCHAIN) $(subst $(CURDIR)/,,$(COMPILE_$(KIND)))'", key)
        link = text[text.index("UNIT_BIN =") : text.index("SLICE =")]
        self.assertIn("read key < $< && $(N64LINK) place build/cas/$$key.o", link.replace("\\\n  ", ""))
        self.assertIn("$(LINK_BIN)", link)
        self.assertIn("--oformat binary -o $@", text[text.index("LINK_BIN =") : text.index("UNIT_BIN =")])
        self.assertIn("build/$1/src/%.i: src/%.c Makefile units.mk | verify build/$1/src build/cas\n", text)

    def test_preprocess_writes_dependencies_in_one_cpp_pass(self) -> None:
        for kind, depend_pass in (("sn64", False), ("ido", True)):
            with self.subTest(kind=kind):
                recipe = buildfiles._kind_recipes(kind)
                preprocess = recipe.splitlines()[0]
                self.assertEqual("-MMD" in preprocess, not depend_pass)
                if kind == "ido":
                    self.assertIn(" -M src/$(*F).c", preprocess)
                    self.assertNotIn("$(CPP)", preprocess)
                else:
                    self.assertIn("-MP -MT $(@D)/$(*F).i -MF $(@D)/$(*F).d", preprocess)
                self.assertTrue(preprocess.endswith("> $(@D)/$(*F).i"))

    def test_flags_with_shell_characters_are_refused(self) -> None:
        with self.assertRaisesRegex(config.Held, "buildfiles.flag"):
            buildfiles.words(["-DX=$(HOME)"])

    def test_n64link_pin_is_the_release_the_host_prints(self) -> None:
        host = self.host
        for printed, refused in (
            (buildfiles.N64LINK_RELEASE, False),
            ("n64link 0.3.0 (SN ASN64 2.81 rules)\n", True),
        ):
            with self.subTest(printed=printed), mock.patch("unbake.process.run_tool", return_value=printed):
                if refused:
                    with self.assertRaisesRegex(config.Held, r"buildfiles.n64link: .* prints 'n64link 0.3.0"):
                        buildfiles.n64link_pin(host)
                else:
                    self.assertEqual(buildfiles.n64link_pin(host), buildfiles.N64LINK_RELEASE)
