"""Make receipt checks keep real object timestamps without shell processes."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unbake.project import makefile


class LinkReceiptTests(unittest.TestCase):
    def test_object_receipt_table(self):
        template = (makefile.TEMPLATES / "Makefile").read_text()
        cases = (
            ("unchanged", b"old", True, True, 0, [], b"old"),
            ("changed", b"new", True, True, 0, ["compile", "link"], b"new"),
            ("same object after source edit", b"old|comment", True, True, 0, ["compile"], b"old"),
            ("cold", b"new", False, False, 0, ["compile", "link"], b"new"),
            ("missing object with warm receipt", b"old", False, True, 2, [], b"old"),
        )
        for kind in ("src", "asm"):
            marker = f"$(BUILD)/obj/{kind}/%.o:"
            rule = marker + template.split(marker, 1)[1].split("\n\n", 1)[0]
            for label, source, object_exists, receipt_exists, status, events, linked in cases:
                with self.subTest(kind=kind, label=label), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    obj = root / f"obj/{kind}/unit.o"
                    receipt = obj.with_suffix(".built")
                    obj.parent.mkdir(parents=True)
                    (root / "source").write_bytes(source)
                    os.utime(
                        root / "source",
                        (100 if label in ("unchanged", "missing object with warm receipt") else 500,) * 2,
                    )
                    if object_exists:
                        obj.write_bytes(b"old")
                        os.utime(obj, (200, 200))
                    if receipt_exists:
                        receipt.write_bytes(b"")
                        os.utime(receipt, (300, 300))
                        (root / "linked").write_bytes(b"old")
                        os.utime(root / "linked", (400, 400))
                    (root / "driver.py").write_text(
                        "import os, sys\nfrom pathlib import Path\n"
                        "action = sys.argv[1]\nobj = Path(sys.argv[2])\n"
                        "with Path('events').open('a') as log: log.write(action + '\\n')\n"
                        "if action == 'compile':\n"
                        "    data = Path('source').read_bytes().split(b'|')[0]\n"
                        "    if not obj.exists() or obj.read_bytes() != data:\n"
                        "        obj.write_bytes(data)\n        os.utime(obj, (550, 550))\n"
                        "    receipt = obj.with_suffix('.built')\n"
                        "    receipt.write_bytes(b'')\n    os.utime(receipt, (600, 600))\n"
                        "else:\n"
                        "    Path('linked').write_bytes(obj.read_bytes())\n"
                        "    os.utime('linked', (700, 700))\n"
                    )
                    python = shlex.quote(sys.executable)
                    object_name = f"./obj/{kind}/unit.o"
                    receipt_name = f"./obj/{kind}/unit.built"
                    (root / "Makefile").write_text(
                        f"BUILD := .\n_WARM_DIRECTORY_CACHE := $(wildcard ./obj/src/*.o ./obj/asm/*.o)\n"
                        f"all: linked\nlinked: {object_name}\n"
                        f"\t{python} driver.py link {object_name}\n"
                        f"{receipt_name}: source\n\t{python} driver.py compile {object_name}\n" + rule + "\n"
                    )
                    result = subprocess.run(["make", "--no-print-directory"], cwd=root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assertEqual((root / "linked").read_bytes(), linked)
                    log = root / "events"
                    self.assertEqual(log.read_text().splitlines() if log.exists() else [], events)
                    if status:
                        self.assertIn("HELD(compile): missing", result.stderr)
