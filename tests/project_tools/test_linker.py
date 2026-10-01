"""Legacy local-rodata builds must retain subsequent ROM-producing code."""

import unittest

from unbake.project_tools.rodata import defer_bss


class LinkerTests(unittest.TestCase):
    def test_local_rodata_does_not_put_remaining_text_in_noload(self) -> None:
        for section in ("text", "data", "rodata", "rdata"):
            with self.subTest(section=section):
                header = (
                    "    }\n    main_bss_VRAM = ADDR(.main_bss);\n"
                    "    .main_bss (NOLOAD) : SUBALIGN(4)\n    {\n"
                    "        FILL(0x00000000);\n        main_BSS_START = .;\n"
                )
                automatic = "        obj/src/local.o(.bss);\n"
                loaded = f"        obj/asm/later.o(.{section});\n"
                resident = "        obj/asm/bss.o(.bss);\n        main_BSS_END = .;\n    }\n"
                prefix = "SECTIONS\n{\n    .main : AT(0x1000)\n    {\n        obj/src/local.o(.rodata);\n"
                script = prefix + header + automatic + loaded + resident + "}\n"
                expected = prefix + loaded + header + automatic + resident + "}\n"
                self.assertEqual(defer_bss(script), expected)
                self.assertEqual(defer_bss(expected), expected)
