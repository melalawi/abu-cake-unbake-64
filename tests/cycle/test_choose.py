"""Named cycle functions skip the size window and each refusal names its one reason."""

from unittest.mock import patch

from tests.kit import TempCase
from tests.project_fixture import make
from unbake.config import Held
from unbake.cycle import engine
from unbake.cycle.rank import Candidate

BIG = Candidate("big", 90000, ("us",), False, None)


class ChooseTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project, self.host = make(self.root, [0x00040000, 0x03E00008, 0x00001021])

    def choose(self, *names: str) -> list[Candidate]:
        with (
            patch("unbake.work.plan.candidates", return_value=[BIG]),
            patch("unbake.work.plan._published_rows", return_value={"clean": ((), None)}),
            patch.object(engine, "ranked", side_effect=AssertionError("the window must not apply")),
        ):
            return engine.choose(self.project, self.host, None, names)

    def test_an_explicit_big_function_is_accepted(self) -> None:
        self.assertEqual(self.choose("big"), [BIG])

    def test_each_refusal_names_the_one_reason_that_applies(self) -> None:
        cases = [
            ("clean", "clean: published and clean"),
            ("nowhere", "nowhere: unknown function"),
            ("alpha", "alpha: us alpha @0x80001000: dead: writes $zero"),
        ]
        for name, expected in cases:
            with self.subTest(name):
                with self.assertRaises(Held) as caught:
                    self.choose(name)
                self.assertEqual(caught.exception.reason, f"cycle.functions: not candidates: {expected}")
