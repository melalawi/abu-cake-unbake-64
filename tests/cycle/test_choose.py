"""Named cycle functions skip the size window and each refusal names its one reason."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from unbake.config import Held
from unbake.cycle import engine
from unbake.cycle.rank import Candidate

BIG = Candidate("big", 90000, ("us",), False, None)


class ChooseTests(unittest.TestCase):
    def choose(self, *names: str) -> list[Candidate]:
        item = SimpleNamespace(name="known", aliases=())
        with (
            patch("unbake.work.plan.candidates", return_value=[BIG]),
            patch("unbake.work.plan._published_rows", return_value={"clean": ((), None)}),
            patch("unbake.work.plan.inventory.inventory", return_value=(None, [item], {})),
            patch.object(engine, "ranked", side_effect=AssertionError("the window must not apply")),
        ):
            return engine.choose(SimpleNamespace(), SimpleNamespace(), None, names)

    def test_an_explicit_big_function_is_accepted(self) -> None:
        self.assertEqual(self.choose("big"), [BIG])

    def test_each_refusal_names_the_one_reason_that_applies(self) -> None:
        cases = [
            ("clean", "clean: published and clean"),
            ("nowhere", "nowhere: unknown function"),
            ("known", "known: not draftable"),
        ]
        for name, expected in cases:
            with self.subTest(name), self.assertRaisesRegex(Held, expected):
                self.choose(name)
