"""Published units that break a source rule are cycle candidates, drafted from their own src/ text."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import config
from unbake.config import Held
from unbake.cycle import rank
from unbake.work import draft, plan

BROKEN = "int alpha(void) { do {} while (0); return 1; }\n"
CLEAN = "int alpha(void) { return 1; }\n"
M2C = "int alpha(void) { return 0; }\n"
WINDOW = {"min_bytes": 4, "max_bytes": 4096, "min_history": 1}


class PublishedUnitTests(ProjectCase):
    def publish(self, function: str, source: str) -> None:
        """The state land leaves: src/F.c and a C row for F in every version."""
        (self.project.src / f"{function}.c").write_text(source)
        for version in self.project.versions:
            split = self.project.version(version).split
            split.write_text(split.read_text().replace(f"asm, {function}]", f"c, {function}]"))
        self.project = config.load(self.project.root)

    def drafted(self) -> str:
        with (
            patch.object(draft.exclusions, "load", return_value=set()),
            patch.object(draft.type_context, "snapshot", return_value=("d", "")),
            patch.object(draft.extract, "directory", return_value=self.root),
            patch.object(draft.m2c, "draft", return_value=M2C) as m2c,
        ):
            made = draft.draft(self.project, self.host, "alpha", replace=False)
        self.assertEqual(m2c.called, made.file.read_text() == M2C)
        return made.file.read_text()

    def test_seed_choice(self) -> None:
        # (src/alpha.c, published row) -> the draft text, or the refusal key
        cases = {
            "unmatched": (None, False, M2C),
            "unmatched with a kept source": (BROKEN, False, M2C),
            "published with findings": (BROKEN, True, BROKEN),
            "published and clean": (CLEAN, True, "draft.published"),
        }
        for name, (source, published, expected) in cases.items():
            with self.subTest(name):
                self.setUp()
                if published:
                    self.publish("alpha", source)
                elif source is not None:
                    (self.project.src / "alpha.c").write_text(source)
                if expected == "draft.published":
                    with self.assertRaises(Held) as raised:
                        self.drafted()
                    self.assertEqual(raised.exception.key, expected)
                    self.assertFalse((self.project.work / "alpha" / "alpha.c").exists())
                else:
                    self.assertEqual(self.drafted(), expected)

    def test_candidates_hold_both_and_the_ranker_mixes_them(self) -> None:
        self.publish("alpha", BROKEN)
        self.publish("beta", CLEAN.replace("alpha", "beta"))
        with (
            patch("unbake.pool.run", lambda host, fn, items: [fn(item) for item in items]),
            patch("unbake.pool.Pool.from_host", return_value=type("P", (), {"size": 2})()),
        ):
            found = {row.function: row for row in plan.candidates(self.project, self.host)}
        self.assertEqual(sorted(found), ["alpha", "gamma"])
        self.assertEqual((found["alpha"].bytes, found["alpha"].versions), (12, ("us", "eu")))
        history = [rank.History("old", 12, True, 1.0)]
        carried = rank.Candidate("alpha", 12, ("us", "eu"), True, 50.0)
        for pool, expected in [
            ([found["gamma"], found["alpha"]], ["alpha", "gamma"]),
            ([found["alpha"], found["gamma"]], ["alpha", "gamma"]),
            ([found["gamma"], carried], ["alpha", "gamma"]),
        ]:
            with self.subTest(pool=[row.function for row in pool]):
                self.assertEqual([row.function for row in rank.rank(pool, history, **WINDOW)], expected)
