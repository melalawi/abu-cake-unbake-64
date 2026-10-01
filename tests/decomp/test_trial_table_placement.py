"""Draft-owned tables must match resident contents, extent, placement, and use."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import assemble
from tests.support import test_policy, tool
from unbake.decomp.score import diff
from unbake.decomp.trial import comparison_identical
from unbake.decomp.trial_compare import compare_object


class TrialTablePlacementTests(unittest.TestCase):
    def test_resident_tables_resolve_only_with_exact_entries_offsets_and_use(self) -> None:
        body = (
            ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
            "lui $at,%hi({first})\nlw {register},%lo({first})($at)\n"
            "lui $at,%hi({second})\nlw $v0,%lo({second})($at)\n"
            ".Lend:\njr $ra\nnop\n.size alpha,.-alpha\n{data}"
        )
        entries = ".section .rdata\n.word .Lend, alpha, alpha, .Lend\n"
        for version, start in (("us", 0x80400000), ("eu", 0x80402000)):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                generation = root / "build" / f"{version}.0"
                generation.mkdir(parents=True)
                policy = test_policy(root)
                baseline = assemble(
                    root, "baseline", body.format(first=".rdata", second=".rdata+8", register="$v0", data=entries)
                )
                script = root / "link.ld"
                script.write_text(
                    f"SECTIONS {{ .text 0x{start:X} : {{ *(.text) }} .rdata 0x800E0000 : {{ *(.rdata) }} }}"
                )
                elf = generation / "game.elf"
                subprocess.run(
                    [tool("mips-linux-gnu-ld"), "-T", str(script), "-o", str(elf), str(baseline)],
                    check=True,
                    capture_output=True,
                )
                (generation / "game.map").write_text(
                    f" 0x{start:X} alpha\n 0x800E0000 first_table\n 0x800E0008 second_table\n"
                )
                target = assemble(
                    root, "target", body.format(first="first_table", second="second_table", register="$v0", data="")
                )
                cases = (
                    ("equal", ".rdata", ".rdata+8", "$v0", entries),
                    ("entry", ".rdata", ".rdata+8", "$v0", entries.replace(".Lend,", ".Lend+4,")),
                    ("offset", ".rdata+4", ".rdata+8", "$v0", entries),
                    ("second offset", ".rdata", ".rdata+12", "$v0", entries),
                    ("register", ".rdata", ".rdata+8", "$v1", entries),
                    ("missing", ".rdata", ".rdata+8", "$v0", entries.replace(", .Lend\n", "\n")),
                    ("extra", ".rdata", ".rdata+8", "$v0", entries + ".word alpha\n"),
                    ("unrelocated", ".rdata", ".rdata+8", "$v0", entries.replace(".Lend,", "0,")),
                    ("unknown placement", ".rdata", ".rdata+8", "$v0", entries),
                    ("unavailable", ".rdata", ".rdata+8", "$v0", entries),
                )
                for name, first, second, register, data in cases:
                    with self.subTest(case=name):
                        candidate = assemble(
                            root, "candidate", body.format(first=first, second=second, register=register, data=data)
                        )
                        if name == "unknown placement":
                            (generation / "game.map").write_text(f" 0x{start:X} alpha\n")
                        elif name == "unavailable":
                            (generation / "game.map").write_text(
                                f" 0x{start:X} alpha\n 0x800E0000 first_table\n 0x800E0008 second_table\n"
                            )
                            elf.unlink()
                        document = diff(
                            policy, version, "alpha", target, candidate, root / "diff.json", generation=generation
                        )
                        result = compare_object(version, document, "alpha")
                        self.assertEqual(comparison_identical(result), name == "equal", result.lines)
                        if name in ("entry", "missing", "extra", "unknown placement", "unavailable"):
                            self.assertGreater(result.typed["relocation"], 0)
                            self.assertIn("jump table", "\n".join(result.lines))
