"""Configured-name review verifies byte pins and preserves data publication."""

import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from unbake.layout import symbol_replan
from unbake.project.config import Held


class RetainedAssertionTests(unittest.TestCase):
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

    def test_retained_name_review_refuses_changed_boundaries(self):
        assertion = {
            "name": "shared",
            "placements": [{"version": "one", "start": 0, "end": 8, "body_sha256": "0" * 64}],
        }
        image = Mock()
        with self.assertRaisesRegex(Held, "boundary changed"):
            symbol_replan.retained_assertions(
                {"one": [SimpleNamespace(start=0, end=4)]}, {"one": image}, [assertion], verify_retained=True
            )
        image.image.assert_not_called()

    def test_retained_name_review_is_an_explicit_setup_option(self):
        from unbake.cli.main import make_parser

        args = make_parser().parse_args(["setup", "--replan-symbols", "--retain-symbol-names"])
        self.assertTrue(args.replan_symbols)
        self.assertTrue(args.retain_symbol_names)

    def test_retained_name_review_preserves_data_bindings_without_replaying_renames(self):
        original = {
            "objects": [{"name": "shared", "placements": [{"version": "one", "address": 123}]}],
            "unified": 1,
            "renames": {"one": {"old": "shared"}},
            "rename_count": 1,
        }
        before = copy.deepcopy(original)
        reviewed = symbol_replan.retained_data_symbols(original)
        self.assertEqual(reviewed["objects"], original["objects"])
        self.assertEqual(reviewed["unified"], 1)
        self.assertEqual(reviewed["renames"], {})
        self.assertEqual(reviewed["rename_count"], 0)
        self.assertEqual(original, before)

    def test_retained_name_publication_never_replays_historical_object_bindings(self):
        data = {"objects": [{"name": "old", "placements": [{"version": "one", "address": 123}]}]}
        for retain in (False, True):
            with self.subTest(retain=retain):
                self.assertEqual(
                    symbol_replan.publication_data({"retain_symbol_names": retain, "data_symbols": data}),
                    {} if retain else data,
                )
        self.assertEqual(data["objects"][0]["name"], "old")
