"""Guide describes runtime storage even when it has no ROM bytes."""

import io
import struct
from contextlib import redirect_stdout

from tests.cli.support import MainCase
from unbake.decomp import guide
from unbake.decomp.rom import project_reader
from unbake.config import Held


class GuideBssTests(MainCase):
    def test_runtime_data_needs_no_file_contents(self) -> None:
        self.source.unlink()
        version = self.project.version("us")
        version.symbols.write_text("")
        for kind, address in (("", 0x801468C0), ("bss", 0x80001010), (".bss", 0x80001010)):
            with self.subTest(kind=kind):
                version.split.write_text(
                    "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
                    "    vram: 0x80001000\n    subalign: 4\n    end: 0x50\n"
                    "    bss_size: 0x20\n    subsegments:\n"
                    "      - [0x40, asm, alpha]\n      - [0x48, data, values]\n"
                    + (f"      - [0x50, {kind}, globals]\n" if kind else "")
                )
                # The ROM ends exactly where the uninitialized row begins.
                target = (0x3C020000 | ((address + 0x8000) >> 16 & 0xFFFF), 0x8C440000 | (address & 0xFFFF))
                version.baserom.write_bytes(bytes(0x40) + struct.pack(">II", *target) + bytes(8))
                with redirect_stdout(io.StringIO()):
                    output = guide.run(self.project, "alpha", "us")
                self.assertIn(f"0x{address:08X}", output)
                if kind:
                    self.assertIn("reference: .bss", output)
                    self.assertIn("size:0x4", output)
                else:
                    self.assertIn("size 0x4; runtime data without ROM contents", output)
                # Raw ROM consumers must still refuse a read of this storage.
                with self.assertRaisesRegex(Held, "unmapped"):
                    project_reader(self.project, "us")(address, 4)
