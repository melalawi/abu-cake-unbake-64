"""A retired split row still has a measured identity inside its current C unit."""

from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake.cycle import engine


class MergedRecheckTests(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "alpha.c").write_text("int alpha(void) { return 1; }\nint beta(void) { return 2; }\n")
        self.versions = {}
        for version in ("us", "eu"):
            layout = self.root / f"{version}.yaml"
            layout.write_text(
                "segments:\n- name: code\n  start: 0\n  vram: 0x80000000\n  subsegments:\n    - [0, c, alpha]\n- [8]\n"
            )
            symbols = self.root / f"{version}.txt"
            symbols.write_text("alpha = 0x80000000; // type:func\nbeta = 0x80000004; // type:func\n")
            rom = self.root / f"{version}.rom"
            rom.write_bytes(bytes(range(8)))
            self.versions[version] = SimpleNamespace(split=layout, symbols=symbols, baserom=rom)
        self.project = SimpleNamespace(src=self.src, versions=tuple(self.versions), version=self.versions.__getitem__)

    def check(self, function="beta", built=bytes(range(8))):
        with (
            patch("unbake.config.load", return_value=self.project),
            patch("unbake.runner.build_unit", return_value=built) as build,
        ):
            result = engine._recheck_task((self.root, SimpleNamespace(), function))
        return result, [(call.args[2], call.args[3]) for call in build.call_args_list]

    def test_inner_function_proves_current_whole_unit_in_both_versions(self):
        result, builds = self.check()
        self.assertEqual(result, {"exact": True, "best_percent": 100.0, "diagnostic": ""})
        self.assertEqual(builds, [("alpha", "us"), ("alpha", "eu")])

    def test_neighbour_byte_change_is_a_real_regression(self):
        result, _ = self.check(built=b"\xff" + bytes(range(1, 8)))
        self.assertFalse(result["exact"])
        self.assertIn("no longer builds identical in us (unit alpha)", result["diagnostic"])

    def test_missing_identity_is_not_success(self):
        result, builds = self.check("absent")
        self.assertFalse(result["exact"])
        self.assertIn("missing", result["diagnostic"])
        self.assertEqual(builds, [])

    def test_source_rules_apply_to_the_containing_unit(self):
        (self.src / "alpha.c").write_text('int alpha(void) { __asm__("nop"); }\n')
        result, _ = self.check()
        self.assertFalse(result["exact"])
        self.assertIn("inline assembly is never allowed", result["diagnostic"])

    def test_assembly_owner_and_ambiguous_identity_still_refuse(self):
        path = self.versions["us"].split
        original = path.read_text()
        path.write_text(original.replace("c, alpha", "asm, alpha"))
        result, builds = self.check()
        self.assertFalse(result["exact"])
        self.assertIn("not C", result["diagnostic"])
        self.assertEqual(builds, [])
        path.write_text(original.replace("- [8]", "    - [4, c, beta]\n- [8]"))
        self.versions["us"].symbols.write_text("alpha = 0x80000000; // type:func\nbeta = 0x80000000; // type:func\n")
        result, builds = self.check()
        self.assertFalse(result["exact"])
        self.assertIn("ambiguous", result["diagnostic"])
        self.assertEqual(builds, [])
