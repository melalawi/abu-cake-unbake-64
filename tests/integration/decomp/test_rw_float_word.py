"""Whole reduced RW outgoing carrier body preserves every IEEE word and call."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.config import Held
from unbake.decomp.draft_macros import lower

CONTEXT = """
float func_8024BEDC_de(int);
void func_8026641C_de(int, int, int, int, int, int, int, int, int, int, void *, int);
"""
BODY = """
void dispatch(int *words, void *shield) {
    func_8026641C_de(words[0], words[1], words[2],
        M2C_BITWISE(int, func_8024BEDC_de(0)), words[4], words[5],
        words[6], words[7], words[8], words[9], shield, words[11]);
}
"""
HARNESS = """
static unsigned int input;
static unsigned int received;
static int producer_calls;
static int dispatch_calls;
static int valid;
float func_8024BEDC_de(int value) {
    union { unsigned int bits; float value; } word;
    producer_calls++;
    word.bits = input;
    return word.value;
}
void func_8026641C_de(int a, int b, int c, int d, int e, int f,
                     int g, int h, int i, int j, void *shield, int flags) {
    dispatch_calls++;
    received = (unsigned int)d;
    valid += a == 1 && b == 2 && c == 3 && e == 5 && f == 6 && g == 7
             && h == 8 && i == 9 && j == 10 && shield == 0 && flags == 12;
}
int main(void) {
    unsigned int patterns[4] = {0x3F800001U, 0xBF800001U, 0x00000001U, 0x7FC12345U};
    int words[12] = {1,2,3,4,5,6,7,8,9,10,11,12};
    int i;
    for (i = 0; i < 4; i++) {
        input = patterns[i];
        dispatch(words, 0);
        if (received != input) return 1;
    }
    return producer_calls != 4 || dispatch_calls != 4 || valid != 4;
}
"""


class RwFloatWordTests(unittest.TestCase):
    def test_all_bits_and_every_outgoing_operand_survive_one_evaluation(self):
        source = lower(BODY, CONTEXT)
        self.assertEqual(source.count("union { unsigned int bits; float value; } word;"), 1)
        self.assertEqual(source.count("func_8024BEDC_de(0)"), 1)
        self.assertEqual(source.count("func_8026641C_de("), 1)
        self.assertNotIn("<< 16", source)
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        run = subprocess.run
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory) / "carrier.c"
            path.write_text(CONTEXT + source + HARNESS)
            with patch("subprocess.run", wraps=run) as work:
                run_compile = subprocess.run(
                    [compiler, "-std=c89", "-Wall", "-Werror", str(path), "-o", directory + "/carrier"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(run_compile.returncode, 0, run_compile.stderr)
                result = subprocess.run([directory + "/carrier"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(work.call_count, 2)

    def test_unknown_or_nonfloat_producer_is_not_reinterpreted(self):
        for context in ("", "int func_8024BEDC_de(int);", "double func_8024BEDC_de(int);"):
            with self.subTest(context=context), self.assertRaisesRegex(Held, "requires addressable value"):
                lower(BODY, context)
