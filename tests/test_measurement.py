"""M10 names target difference, insertion and unavailable native observations independently."""

import struct
import unittest

from unbake.process import Fault, named
from unbake.work.score import Measurement, measure_words, unavailable


class MeasurementTests(unittest.TestCase):
    def test_insertions_never_change_the_target_denominator(self):
        measured = measure_words(
            "us", struct.pack(">2I", 0x24420004, 0x03E00008), struct.pack(">3I", 0x24420004, 0, 0x03E00008)
        )
        self.assertEqual(
            (
                measured.identical_words,
                measured.target_words,
                measured.candidate_words,
                measured.target_words_different,
                measured.inserted_words,
            ),
            (2, 2, 3, 0, 1),
        )
        self.assertFalse(measured.exact)
        self.assertEqual(Measurement.read(measured.document()).document(), measured.document())

    def test_unavailable_is_null_and_valid_zero_is_available(self):
        missing = unavailable(
            "us", 1, Fault(named("compile.provider", "provider absent", owner="family", stage="preprocess"))
        )
        self.assertEqual((missing.percent, missing.identical_words, missing.target_words_different), (None, None, None))
        self.assertFalse(missing.available)
        zero = measure_words("us", bytes.fromhex("24420004"), bytes(4))
        self.assertEqual((zero.available, zero.percent, zero.target_words_different), (True, 0.0, 1))

    def test_bool_count_unknown_availability_and_missing_fault_refuse(self):
        valid = measure_words("us", bytes.fromhex("24420004"), bytes(4)).document()
        for field, value in (("target_words", True), ("available", 1), ("identical_words", -1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                Measurement.read({**valid, field: value})
