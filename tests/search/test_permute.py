import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import cast
from unittest.mock import patch

from unbake.decomp import explain, trial
from unbake.decomp.explain import Allocation
from unbake.decomp.trial import Trial
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.project import makefile, toolchain
from unbake.config import Held, Policy, Project
from unbake.search import core, permute
from unbake.search.core import Context

SOURCE = """void func_802C4F58(int *arg0, int arg1) {
    int i, j2, k, tmp, n2;
    n2 = arg1 * 2;
    for (i = 0; i < arg1; i++) arg0[i] = i;
    j2 = 1;
    for (i = 1; n2 >= i; i += 2) {
        if (j2 > i) {
            tmp = arg0[(j2 - 1) / 2];
            arg0[(j2 - 1) / 2] = arg0[(i - 1) / 2];
            arg0[(i - 1) / 2] = tmp;
        }
        k = arg1;
        while (k >= 2 && j2 > k) { j2 -= k; k >>= 1; }
        j2 += k;
    }
}
"""

EXTERNAL = """import pathlib, subprocess, sys
work = pathlib.Path(sys.argv[1])
(work / 'arguments.json').write_text(__import__('json').dumps(sys.argv[2:]))
print('[fixture] base score = 8', flush=True)
subprocess.run([str(work / 'compile.sh'), str(work / 'base.c'), '-o', str(work / 'probe.o')], check=True)
source = (work / 'base.c').read_text()
for score, text in [
    (8, source + '/* tie */'), (3, source.replace('j2', 'j')),
    (0, source.replace('j2', 'j') + '/* zero */'), (0, source + '/* refused */')
]:
    out = work / ('output-%d-%d' % (score, len(list(work.glob('output-*'))) + 1))
    out.mkdir()
    (out / 'score.txt').write_text(str(score))
    (out / 'source.c').write_text(text)
"""

HELPER = """import argparse, json, pathlib, sys
p = argparse.ArgumentParser()
for name in ('kind', 'recipe', 'version', 'unit', 'cache-root', 'source', 'output', 'non-matching'):
    p.add_argument('--' + name, required=True)
a = p.parse_args()
pathlib.Path(a.output).write_bytes(bytes.fromhex('03e0000800000000'))
pathlib.Path(a.output + '.json').write_text(json.dumps(vars(a)))
"""


class PermuteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        root = self.home / "project"
        (root / "src").mkdir(parents=True)
        (root / "tools").mkdir()
        (root / "tools/compiler.sha256").write_text("explicit fixture pin\n")
        self.cpp = self.home / "preprocess"
        self.cpp.write_text(
            "#!" + sys.executable + '\nimport pathlib, sys\nprint(pathlib.Path(sys.argv[-1]).read_text(), end="")\n'
        )
        self.cpp.chmod(0o755)
        self.compiler = NS(id="fixture", cc=self.cpp)
        self.project = NS(
            root=root,
            src=root / "src",
            tools=root / "tools",
            version=lambda v: NS(name=v),
            compiler_for=lambda p: self.compiler,
        )
        self.archive = self.home / "dependency.tar"
        with tarfile.open(self.archive, "w") as archive:
            content = EXTERNAL.encode()
            member = tarfile.TarInfo("release/permuter.py")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        self.policy = NS(
            permuter_archive=self.archive,
            permuter_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            cache_root=self.home / "cache",
            cores=1,
            search_beam=1,
            stall_trials=1,
        )
        self.target = self.home / "target.o"
        # First three instructions of the real function: addu t2,a0,zero;
        # addu t1,a1,zero; addu a3,zero,zero.
        self.target.write_bytes(bytes.fromhex("0080502100a0482100003821"))
        self.ctx = Context(
            cast(Project, self.project),
            cast(Policy, self.policy),
            self.home / "out",
            root / "src/func_802C4F58.c",
            Allocation((), (), (), ()),
            (),
            time.monotonic() + 30,
        )
        self.trial = cast(Trial, NS(function="func_802C4F58", compares={"us": NS(), "eu-x": NS()}))
        self.generator = permute.Permuter("us", self.target, 5)
        import shlex

        from tests.preprocessor import output as preprocessing
        from tests.process_fakes import boundary
        from unbake.decomp import trial_compile

        process = boundary(trial_compile, preprocessing)
        process.start()
        self.addCleanup(process.stop)

        def launch(command, **kwargs):
            work = Path(command[3])
            entry = Path(command[2]).read_text()
            if "fixture cannot start" in entry:
                kwargs["stderr"].write(b"fixture cannot start\n")
                return NS(wait=lambda **options: 23)
            (work / "arguments.json").write_text(json.dumps(command[4:]))
            kwargs["stdout"].write(b"[fixture] base score = 8\n")
            script = (work / "compile.sh").read_text()
            arguments = shlex.split(script.split("exec ", 1)[1].split(" --source", 1)[0])
            metadata = {arguments[i][2:].replace("-", "_"): arguments[i + 1] for i in range(2, len(arguments), 2)}
            (work / "probe.o.json").write_text(json.dumps(metadata))
            source = (work / "base.c").read_text()
            for index, (score, text) in enumerate(
                (
                    (8, source + "/* tie */"),
                    (3, source.replace("j2", "j")),
                    (0, source.replace("j2", "j") + "/* zero */"),
                    (0, source + "/* refused */"),
                ),
                1,
            ):
                destination = work / f"output-{score}-{index}"
                destination.mkdir()
                (destination / "score.txt").write_text(str(score))
                (destination / "source.c").write_text(text)
            return NS(wait=lambda **options: 0)

        from types import SimpleNamespace

        process = patch.object(permute, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": launch}))
        process.start()
        self.addCleanup(process.stop)
        self.addCleanup(patch.stopall)
        patch.object(toolchain, "verify", return_value={}).start()
        self.spec = patch.object(toolchain, "specification", return_value=NS(family="gcc")).start()
        patch.object(makefile, "helpers", return_value={"tools/compile.py": HELPER, "tools/build.json": "{}"}).start()

    def test_early_exit_reports_permuter_status_and_stderr(self) -> None:
        with tarfile.open(self.archive, "w") as archive:
            content = b"import sys\nprint('fixture cannot start', file=sys.stderr)\nsys.exit(23)\n"
            member = tarfile.TarInfo("release/permuter.py")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        self.policy.permuter_sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with self.assertRaisesRegex(Held, r"permuter .*permuter\.py exited 23: fixture cannot start") as refusal:
            list(self.generator.propose(SOURCE, self.trial, self.ctx))
        self.assertEqual(refusal.exception.phase, "permute")
        self.assertTrue(self.generator.ran)
        work = next(self.ctx.out.glob("permute-*"))
        self.assertEqual((work / "permuter.log").read_bytes(), b"")
        self.assertEqual((work / "permuter.log.stderr").read_text(), "fixture cannot start\n")

    def test_setup_exhausts_budget_without_launching_or_reading_missing_log(self) -> None:
        clock = [time.monotonic()]
        deadline = clock[0] + 30

        # Capture the real checkout before patching the module attribute.
        real_checkout = permute.checkout

        def exhausted_checkout(archive: Path, digest: str, work: Path) -> Path:
            entry = real_checkout(archive, digest, work)
            clock[0] = deadline
            return entry

        with (
            patch.object(time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(permute, "checkout", side_effect=exhausted_checkout),
            patch.object(subprocess, "Popen") as launch,
        ):
            self.assertEqual(list(self.generator.propose(SOURCE, self.trial, self.ctx)), [])
        launch.assert_not_called()
        self.assertFalse(self.generator.ran)
        work = next(self.ctx.out.glob("permute-*"))
        self.assertEqual((work / "permuter.log").read_bytes(), b"")
        self.assertEqual((work / "permuter.log.stderr").read_bytes(), b"")

    def test_successful_permuter_search_selects_confirmed_best_result(self) -> None:
        source = self.home / "func_802C4F58.c"
        source.write_text(SOURCE)
        calls: list[list[str] | None] = []

        def measure(
            project: Project, policy: Policy, path: Path, scratch: Path, versions: list[str] | None = None
        ) -> Trial:
            text = path.read_text()
            calls.append(versions)
            if "/* refused */" in text:
                raise Held("try", "fixture rejected")
            score = 1 if "j2" in text else (2 if "/* zero */" in text else 3)
            compares = {
                version: Compare(version, score, 3, dict.fromkeys(TYPES, 0), [], 100 * score / 3, ())
                for version in (versions if versions is not None else ("us", "eu-x"))
            }
            return Trial(source.stem, hashlib.sha256(path.read_bytes()).hexdigest(), compares, [], "try again")

        with (
            patch.object(
                core, "preprocess", side_effect=lambda project, policy, path, version, deadline: path.read_text()
            ),
            patch.object(explain, "allocation", return_value=Allocation((), (), (), ())),
            patch.object(core, "_retain", return_value=100.0),
            patch.object(trial, "try_draft", side_effect=measure),
        ):
            result = core.run(self.ctx.project, self.ctx.policy, source, [self.generator], self.ctx.out, 5)
        self.assertTrue(self.generator.ran)
        self.assertEqual(result.source.read_text(), SOURCE.replace("j2", "j"))
        self.assertEqual(result.score, 3)
        self.assertEqual(set(result.trial.compares), {"us", "eu-x"})
        self.assertEqual(calls, [None, ["us"], None, ["us"], ["us"], None])

    def test_family_recipe_and_improvements_pass_to_core(self) -> None:
        for family in ("gcc", "ido"):
            with self.subTest(family=family):
                self.spec.return_value = NS(family=family)
                mutations = list(self.generator.propose(SOURCE, self.trial, self.ctx))
                self.assertEqual(len(mutations), 3)
                self.assertTrue(all(m.kind == "permute" for m in mutations))
                work = max(self.ctx.out.glob("permute-*"), key=lambda p: p.stat().st_mtime_ns)
                probe = json.loads((work / "probe.o.json").read_text())
                self.assertEqual(
                    (probe["unit"], probe["version"], probe["non_matching"]), ("src/func_802C4F58.c", "us", "0")
                )
                self.assertEqual(Path(probe["cache_root"]), self.policy.cache_root)
                self.assertEqual((work / "base.c").read_text(), SOURCE)
                self.assertEqual((work / "target.o").read_bytes(), self.target.read_bytes())

    def test_missing_permuter_dependencies_are_refused_by_name(self) -> None:
        for dependency in ("pycparser", "toml"):
            with (
                self.subTest(dependency=dependency),
                patch.object(
                    importlib.util,
                    "find_spec",
                    side_effect=lambda name, missing=dependency: None if name == missing else object(),
                ),
            ):
                with self.assertRaisesRegex(Held, f"dependency {dependency}: missing from interpreter"):
                    list(self.generator.propose(SOURCE, self.trial, self.ctx))
                self.assertFalse(self.ctx.out.exists())

    def test_assembly_target_uses_supported_branch_scoring_flags(self) -> None:
        list(self.generator.propose(SOURCE, self.trial, self.ctx))
        work = next(self.ctx.out.glob("permute-*"))
        arguments = json.loads((work / "arguments.json").read_text())
        self.assertNotIn("--no-ignore-branch-targets", arguments)
        self.assertIn("--stack-diffs", arguments)
        self.assertEqual((work / "target.o").read_bytes(), self.target.read_bytes())

    def test_refusals_name_missing_or_invalid_value(self) -> None:
        rows: list[tuple[str, str, object]] = [
            ("context", "deadline", None),
            ("context", "deadline", float("inf")),
            ("policy", "permuter_archive", None),
            ("policy", "permuter_sha256", None),
            ("policy", "permuter_archive", self.home / "absent.tar"),
            ("policy", "cores", 0),
            ("policy", "cores", True),
            ("policy", "cache_root", None),
            ("policy", "permuter_sha256", "bad"),
            ("policy", "permuter_sha256", "0" * 64),
            ("trial", "function", None),
            ("trial", "compares", {}),
            ("generator", "version", None),
            ("generator", "target_object", None),
            ("generator", "budget_seconds", None),
            ("generator", "budget_seconds", 0),
            ("generator", "budget_seconds", True),
            ("generator", "budget_seconds", float("inf")),
            ("generator", "target_object", self.home / "absent.o"),
        ]
        for owner, name, value in rows:
            with self.subTest(owner=owner, name=name, value=value):
                obj = {"policy": self.policy, "trial": self.trial, "generator": self.generator, "context": self.ctx}[
                    owner
                ]
                previous = getattr(obj, name)
                object.__setattr__(obj, name, value)
                try:
                    with self.assertRaisesRegex(Held, name):
                        list(self.generator.propose(SOURCE, self.trial, self.ctx))
                finally:
                    object.__setattr__(obj, name, previous)

    def test_output_and_cache_cannot_write_inside_project(self) -> None:
        for name in ("out", "cache_root"):
            with self.subTest(name=name):
                owner = self.ctx if name == "out" else self.policy
                previous = getattr(owner, name)
                object.__setattr__(owner, name, self.project.root / "output")
                try:
                    with self.assertRaisesRegex(Held, name):
                        list(self.generator.propose(SOURCE, self.trial, self.ctx))
                finally:
                    object.__setattr__(owner, name, previous)

    def test_archive_members_cannot_escape_output(self) -> None:
        for name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                archive = self.home / "unsafe.tar"
                with tarfile.open(archive, "w") as stream:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/outside"
                    stream.addfile(member)
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                work = self.home / ("unsafe-" + kind.decode())
                work.mkdir()
                with self.assertRaisesRegex(Held, "unsafe member"):
                    permute.checkout(archive, digest, work)

    def test_archive_entry_is_the_top_level_permuter(self) -> None:
        # The released archive ships the entry point and a same-named library module under src/.
        archive = self.home / "release.tar"
        with tarfile.open(archive, "w") as stream:
            for name in ("permuter-1/permuter.py", "permuter-1/src/permuter.py"):
                data = b"print('entry')\n"
                member = tarfile.TarInfo(name)
                member.size = len(data)
                stream.addfile(member, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        work = self.home / "release"
        work.mkdir()
        entry = permute.checkout(archive, digest, work)
        self.assertEqual(entry, work / "dependency" / "permuter-1" / "permuter.py")

    def test_results_require_matching_score_and_source(self) -> None:
        for score, source in (("bad", SOURCE), ("2", SOURCE), ("0", None)):
            with self.subTest(score=score, source=source is None):
                work = Path(tempfile.mkdtemp(dir=self.home))
                directory = work / "output-0-1"
                directory.mkdir()
                (directory / "score.txt").write_text(score)
                if source is not None:
                    (directory / "source.c").write_text(source)
                with self.assertRaises(Held):
                    list(permute.outputs(work))

    def test_compile_script_confines_output_and_preserves_quoted_paths(self) -> None:
        work = self.home / "search with spaces"
        (work / "recipe").mkdir(parents=True)
        (work / "recipe/compile.py").write_text(HELPER)
        (work / "recipe/build.json").write_text("{}")
        script = work / "compile.sh"
        script.write_text(permute.compile_script(self.ctx.project, self.ctx.policy, self.ctx.source, "us", work))
        source = work / "input with spaces.c"
        source.write_text(SOURCE)
        import shlex

        content = script.read_text()
        self.assertIn("output outside search directory", content)
        self.assertIn(shlex.quote(str(work)), content)
        self.assertIn(shlex.quote(str(self.project.root)), content)
        self.assertIn('"$source" --output "$output"', content)
