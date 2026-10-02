"""Native mixed-pool identity uses the build validator without mutating drafts."""

import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import assemble
from tests.support import test_policy
from unbake.decomp.score import diff
from unbake.decomp.trial import comparison_identical
from unbake.decomp.trial_compare import compare_object
from unbake.decomp.trial_target import require_symbol_boundary
from unbake.project.config import Held, load
from unbake.project_tools.elf import Object


class NativePoolTests(unittest.TestCase):
    def test_duplicate_literals_gap_and_table_require_byte_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copyfile(Path(__file__).parents[1] / "fixture/config.toml", root / "config.toml")
            version = root / "versions/us"
            version.mkdir(parents=True)
            (version / "fixture.yaml").write_text(
                "name: fixture\nsegments:\n"
                "  - name: code\n    type: code\n    start: 0x40\n    vram: 0x80002000\n"
                "    subsegments:\n      - [0x40, asm, alpha]\n"
                "  - name: constants\n    type: code\n    start: 0x100\n    vram: 0x80001000\n"
                "    subsegments:\n      - [0x100, data, pool]\n  - [0x118]\n"
            )
            (version / "symbol_addrs.txt").write_text(
                "alpha = 0x80002000;\nfirst = 0x80001004;\nsecond = 0x8000100C;\ntable = 0x80001010;\n"
            )
            native = [0x3C018000, 0xC4201004, 0x3C018000, 0xC422100C, 0x3C018000, 0x24221010, 0x03E00008, 0]
            image = bytearray(0x118)
            image[0x40:0x60] = struct.pack(">8I", *native)
            image[0x100:0x118] = struct.pack(">6I", 0, 0x3F800000, 0x12345678, 0x3F800000, 0x80002018, 0x80002018)
            (root / "roms").mkdir(exist_ok=True)
            (root / "roms/baserom.us.z64").write_bytes(image)
            generation = root / "build/us.0"
            generation.mkdir(parents=True)
            body = (
                ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                "lui $at,%hi({first})\nlwc1 $f0,%lo({first})($at)\n"
                "lui $at,%hi({second})\nlwc1 {register},%lo({second})($at)\n"
                "lui $at,%hi({table})\naddiu $v0,$at,%lo({table})\n"
                "case:\njr $ra\nnop\n.size alpha,.-alpha\n{data}"
            )
            target = assemble(
                root, "target", body.format(first="first", second="second", table="table", register="$f2", data="")
            )
            pool = ".section .rdata\nliteral: .word 0x3f800000\n.align 3\njt: .word case,case\n"
            for name, register, data in (
                ("equal", "$f2", pool),
                ("register", "$f4", pool),
                ("literal", "$f2", pool.replace("0x3f800000", "0x40000000")),
                ("entry", "$f2", pool.replace("case,case", "case+4,case")),
                ("missing", "$f2", pool.replace("case,case", "case")),
                ("extra", "$f2", pool.replace("case,case", "case,case,case")),
                ("rodata", "$f2", pool.replace(".rdata", ".rodata")),
                ("rodata missing", "$f2", pool.replace(".rdata", ".rodata").replace("case,case", "case")),
            ):
                with self.subTest(case=name):
                    candidate = assemble(
                        root,
                        "candidate",
                        body.format(first="literal", second="literal", table="jt", register=register, data=data),
                    )
                    before = candidate.read_bytes()
                    document = diff(
                        test_policy(root), "us", "alpha", target, candidate, root / "diff.json", generation=generation
                    )
                    result = compare_object("us", document, "alpha")
                    self.assertEqual(candidate.read_bytes(), before)
                    self.assertEqual(comparison_identical(result), name in ("equal", "rodata"), result.lines)
                    if name in ("equal", "rodata"):
                        section = ".rodata" if name == "rodata" else ".rdata"
                        proof = document["pool_placement"]
                        self.assertEqual(proof["sections"][f"[{section}]"], 0x80001004)
                        placed = Object(proof["object"])
                        self.assertEqual(len(placed.content(placed.section(section))), 20)
                        self.assertEqual(result.typed["relocation"], 0)
                    if name in ("missing", "rodata missing"):
                        self.assertIn("missing draft entry", "\n".join(result.lines))

            # A native split owns the interval, while a second function symbol
            # can still truncate the entry inside that same object.
            target = assemble(
                root,
                "boundary",
                ".text\n.globl alpha\n.type alpha,@function\n"
                "alpha: nop\n.size alpha,.-alpha\n.globl tail\n.type tail,@function\n"
                "tail: jr $ra\nnop\n.size tail,.-tail\n",
            )
            with self.assertRaisesRegex(Held, "precondition split-boundary.*tail"):
                require_symbol_boundary(load(root), "alpha", "us", target)
