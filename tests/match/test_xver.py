"""Submission proves all named owners and leaves differently named units alone."""

from tests.match.support import MatchFixture
from unbake.match import queue as match
from unbake.project.config import Held


class CrossVersionTests(MatchFixture):
    def test_equal_named_owners_require_trials_on_both_versions(self) -> None:
        source = self.draft("alpha", versions=["us"])
        with self.assertRaisesRegex(Held, "submit.versions"):
            match.submit(self.project, self.policy, source)
        self.prove(source)
        receipts = match.publish_source(self.project, self.policy, source)
        self.assertTrue(any("matched on VERSION us, eu" in line for line in receipts), receipts)

    def test_different_named_function_is_not_an_implicit_owner(self) -> None:
        cartridge = self.project.version("eu")
        cartridge.split.write_text(cartridge.split.read_text().replace("text/alpha", "eu_alpha"))
        source = self.draft("alpha", versions=["us"])
        receipts = match.publish_source(self.project, self.policy, source)
        self.assertTrue(any("matched on VERSION us" in line for line in receipts), receipts)
        self.assertIn("asm, eu_alpha", cartridge.split.read_text())
        self.assertEqual(self.current(self.project, "eu"), self.original["eu"])
