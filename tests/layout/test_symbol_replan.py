"""Replan reviews obsolete boundaries without discarding their byte pins."""

import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from unbake.layout import symbol_replan
from unbake.project.config import Held


class RetainedAssertionTests(unittest.TestCase):
    def test_boundary_reviews_table_verify_every_member_of_retired_groups(self):
        image = b"abcdefghijkl"
        assertion = {
            "name": "shared",
            "placements": [
                {"version": v, "start": 0, "end": 8, "body_sha256": hashlib.sha256(image[:8]).hexdigest()}
                for v in ("one", "two")
            ],
        }
        for end in (8, 4, None):
            with self.subTest(end=end):
                functions = {
                    "one": [SimpleNamespace(start=0, end=end)] if end is not None else [],
                    "two": [SimpleNamespace(start=0, end=8)],
                }
                images = {v: Mock(image=Mock(return_value=image)) for v in functions}
                original = copy.deepcopy(assertion)
                retained, retired = symbol_replan.retained_assertions(functions, images, [assertion])
                self.assertEqual(assertion, original)
                if end == 8:
                    self.assertEqual(retained, [assertion])
                    self.assertFalse(retired)
                    for cartridge in images.values():
                        cartridge.image.assert_not_called()
                else:
                    self.assertFalse(retained)
                    self.assertEqual(retired, [{"assertion": assertion, "reason": "boundary changed"}])
                    for cartridge in images.values():
                        cartridge.image.assert_called_once_with()

    def test_retirement_refuses_bad_old_pins_including_unchanged_members(self):
        image = b"abcdefghijkl"
        functions = {v: [SimpleNamespace(start=0, end=4 if v == "one" else 8)] for v in ("one", "two")}
        for bad_version in ("one", "two"):
            with self.subTest(version=bad_version):
                assertions = [
                    {
                        "name": "shared",
                        "placements": [
                            {
                                "version": v,
                                "start": 0,
                                "end": 8,
                                "body_sha256": "0" * 64 if v == bad_version else hashlib.sha256(image[:8]).hexdigest(),
                            }
                            for v in functions
                        ],
                    }
                ]
                original = copy.deepcopy(assertions)
                with self.assertRaisesRegex(Held, "symbol_assertion_stale.*bytes changed"):
                    symbol_replan.retained_assertions(
                        functions, {v: Mock(image=Mock(return_value=image)) for v in functions}, assertions
                    )
                self.assertEqual(assertions, original)

    def test_retirement_refuses_unknown_version_and_out_of_image_extent(self):
        for version, end in (("missing", 8), ("one", 32)):
            with self.subTest(version=version, end=end):
                assertion = {
                    "name": "shared",
                    "placements": [{"version": version, "start": 0, "end": end, "body_sha256": "0" * 64}],
                }
                with self.assertRaisesRegex(Held, "symbol_assertion_stale"):
                    symbol_replan.retained_assertions(
                        {"one": []}, {"one": Mock(image=Mock(return_value=b"abcdefgh"))}, [assertion]
                    )

    def test_retained_name_review_verifies_unchanged_group_pins(self):
        for digest, expected in ((hashlib.sha256(b"abcdefgh").hexdigest(), True), ("0" * 64, False)):
            with self.subTest(valid=expected):
                assertion = {
                    "name": "shared",
                    "placements": [{"version": "one", "start": 0, "end": 8, "body_sha256": digest}],
                }
                image = Mock(image=Mock(return_value=b"abcdefgh"))
                if expected:
                    kept, retired = symbol_replan.retained_assertions(
                        {"one": [SimpleNamespace(start=0, end=8)]}, {"one": image}, [assertion], verify_retained=True
                    )
                    self.assertEqual(kept, [assertion])
                    self.assertFalse(retired)
                else:
                    with self.assertRaisesRegex(Held, "bytes changed"):
                        symbol_replan.retained_assertions(
                            {"one": [SimpleNamespace(start=0, end=8)]},
                            {"one": image},
                            [assertion],
                            verify_retained=True,
                        )
                image.image.assert_called_once_with()

    def test_retained_name_review_is_an_explicit_setup_option(self):
        from unbake.cli.main import make_parser

        args = make_parser().parse_args(["setup", "--replan-symbols", "--retain-symbol-names"])
        self.assertTrue(args.replan_symbols)
        self.assertTrue(args.retain_symbol_names)
