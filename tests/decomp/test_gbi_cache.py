"""GBI proofs share compiler artifacts with ordinary builds."""

import tempfile
import unittest
from pathlib import Path

from tests.project.makefile_fixture import fixture
from unbake.decomp import gbi_proof
from unbake.project import build


class GbiCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        from unittest.mock import patch

        patch.stopall()

    def test_repeated_proof_and_build_compile_each_content_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy = fixture(root, case=self)
            unit = project.src / "middle.c"
            before, after = "int middle(void) { return 1; }", "int middle(void) { return 2; }"
            gbi_proof.preserve(project, policy, unit, before, after)
            self.assertEqual((root / "calls").read_text().splitlines(), ["cc", "cc"])
            gbi_proof.preserve(project, policy, unit, before, after)
            unit.write_text(after)
            build.compile_object(project, policy, unit, "us", root / "ordinary.o")
            self.assertEqual((root / "calls").read_text().splitlines(), ["cc", "cc"])

    def test_proof_checks_guarded_code_and_every_owning_version(self) -> None:
        from typing import cast
        from unittest.mock import patch

        from tests.decomp.support import assemble
        from tests.decomp.support import fixture as decomp_fixture
        from unbake.project.config import Held, Policy

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, policy, _ = decomp_fixture(root, versions=("us", "eu"), case=self)
            unit = project.src / "alpha.c"
            for differing_version, guarded in (("us", True), ("eu", False)):
                calls = []

                def compile_source(
                    project,
                    policy,
                    source,
                    version,
                    output,
                    *,
                    non_matching=False,
                    calls=calls,
                    differing_version=differing_version,
                    guarded=guarded,
                ):
                    calls.append((version, non_matching, source.parent.name))
                    different = (
                        version == differing_version and non_matching == guarded and source.read_text() == "after"
                    )
                    output.parent.mkdir(parents=True, exist_ok=True)
                    return assemble(output.parent, output.stem, ".text\n.word " + ("1" if different else "0") + "\n")

                with (
                    patch.object(build, "compile_object", side_effect=compile_source),
                    self.assertRaisesRegex(Held, f"VERSION {differing_version} NON_MATCHING={int(guarded)}"),
                ):
                    gbi_proof.preserve(project, cast(Policy, policy), unit, "before", "after")
                self.assertIn((differing_version, guarded, "after"), calls)
                if differing_version == "eu":
                    self.assertEqual({mode for version, mode, _ in calls if version == "us"}, {False, True})
