"""An empty submit is a real retained-object cartridge proof."""

from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli.main import make_parser
from unbake.match import batch, relink
from unbake.project import build, makefile
from unbake.project.config import Held


class BaselineTests(MatchFixture):
    def test_empty_submit_proves_every_version_without_compiling_or_publishing(self):
        for failed in (None, "eu"):
            with self.subTest(failed=failed):
                before = {v: self.project.build_link(v).resolve() for v in self.versions}
                objects = {v: (p / "object.o").read_bytes() for v, p in before.items()}
                visited = []

                def prove(
                    project,
                    policy,
                    version,
                    generation,
                    retained,
                    *,
                    extracted,
                    before=before,
                    objects=objects,
                    visited=visited,
                    failed=failed,
                ):
                    self.assertIs(project, self.project)
                    self.assertIs(policy, self.policy)
                    self.assertIsNone(retained)
                    self.assertTrue(extracted)
                    self.assertNotEqual(generation, before[version])
                    self.assertEqual((generation / "object.o").read_bytes(), objects[version])
                    visited.append(version)
                    return build.BuildResult(
                        version, version != failed, f"{version}: OK", generation / "build.log", generation
                    )

                with (
                    patch.object(relink, "prove", side_effect=prove),
                    patch.object(build, "compile_versions") as compile,
                ):
                    if failed:
                        with self.assertRaisesRegex(Held, "unchanged baseline failed on eu"):
                            batch.publish(self.project, self.policy, [])
                    else:
                        receipts = batch.publish(self.project, self.policy, [])
                        self.assertEqual(receipts, [f"OK(submit): {v}: {v}: OK" for v in self.versions])
                    compile.assert_not_called()
                self.assertCountEqual(visited, self.versions)
                self.assertEqual({v: self.project.build_link(v).resolve() for v in self.versions}, before)
                self.assertEqual({v: (p / "object.o").read_bytes() for v, p in before.items()}, objects)
                for relative, content in makefile.helpers(self.project).items():
                    self.assertEqual((self.root / relative).read_text(), content)

    def test_public_parser_accepts_empty_batch(self):
        args = make_parser().parse_args(["submit", "--batch"])
        self.assertEqual(args.batch, [])
        self.assertIsNone(args.source)
