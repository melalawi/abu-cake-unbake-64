"""Free-match decisions must be visible before the batch publication starts."""

from __future__ import annotations

from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.match import free, queue
from unbake.project.config import Held, Policy, Project


class FreeReportingTests(MatchFixture):
    def test_streams_named_decisions_and_summarizes_publication_outcomes(self) -> None:
        for outcome in ("accepted", "refused", "raised"):
            with self.subTest(outcome=outcome):
                for name in ("alpha", "beta", "gamma"):
                    (self.src / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
                emitted: list[str] = []

                def submit(
                    project: Project,
                    policy: Policy,
                    source: object,
                    *,
                    versions: tuple[str, ...],
                    visible: list[str] = emitted,
                ) -> list[str]:
                    self.assertEqual(versions, ("us",))
                    name = str(source).rsplit("/", 1)[-1].removesuffix(".c")
                    if name == "beta":
                        self.assertTrue(any("alpha queued" in line for line in visible))
                        raise Held("match", "beta: unresolved symbol missing_name")
                    if name == "gamma":
                        raise Held("match", "gamma: requires identical_everywhere=true")
                    return ["OK(match): alpha queued exact-bytes"]

                def publish(
                    project: Project, policy: Policy, visible: list[str] = emitted, result: str = outcome
                ) -> list[str]:
                    self.assertTrue(any("beta: unresolved symbol missing_name" in line for line in visible))
                    self.assertTrue(any("skipped gamma" in line for line in visible))
                    self.assertTrue(any("publication started" in line for line in visible))
                    if result == "raised":
                        raise Held("build", "named build failure")
                    if result == "refused":
                        return ["HELD(match): alpha: ROM compare failed"]
                    return ["OK(match): alpha matched on VERSION us"]

                with (
                    patch.object(queue, "submit", side_effect=submit),
                    patch.object(queue, "status", return_value=["alpha pending"]),
                    patch.object(queue, "run", side_effect=publish),
                ):
                    if outcome == "raised":
                        with self.assertRaisesRegex(Held, "named build failure"):
                            free.land(self.project, self.policy, "us", report=emitted.append)
                    else:
                        self.assertEqual(free.land(self.project, self.policy, "us", report=emitted.append), emitted)
                self.assertIn("publication finished; wall=", emitted[-2])
                self.assertIn("summary VERSION us: eligible=3 queued=1", emitted[-1])
                self.assertIn(f"accepted={int(outcome == 'accepted')} skipped=1", emitted[-1])
                for step in ("scan VERSION", "step=submit", "queue status", "publication finished", "summary VERSION"):
                    self.assertTrue(any(step in line and "wall=" in line for line in emitted), step)
