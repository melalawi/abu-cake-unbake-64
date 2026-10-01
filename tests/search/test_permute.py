import hashlib
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
from unittest.mock import patch

from unbake.decomp.explain import Allocation
from unbake.project.config import Held
from unbake.search import permute
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
        self.home = Path(temporary.name)
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
        )
        self.target = self.home / "target.o"
        # First three instructions of the real function: addu t2,a0,zero;
        # addu t1,a1,zero; addu a3,zero,zero.
        self.target.write_bytes(bytes.fromhex("0080502100a0482100003821"))
        self.ctx = Context(
            self.project,
            self.policy,
            self.home / "out",
            root / "src/func_802C4F58.c",
            Allocation((), (), (), ()),
            (),
            time.monotonic() + 30,
        )
        self.trial = NS(function="func_802C4F58", compares={"us": NS(), "eu-x": NS()})
        self.generator = permute.Permuter("us", self.target, 5)
        self.addCleanup(patch.stopall)
        patch.object(permute.toolchain, "verify", return_value={}).start()
        self.spec = patch.object(permute.toolchain, "specification", return_value=NS(family="gcc")).start()
        patch.object(
            permute.makefile, "helpers", return_value={"tools/compile.py": HELPER, "tools/build.json": "{}"}
        ).start()

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

    def test_refusals_name_missing_or_invalid_value(self) -> None:
        rows = [
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
        script.write_text(permute.compile_script(self.project, self.policy, self.ctx.source, "us", work))
        source = work / "input with spaces.c"
        source.write_text(SOURCE)
        for output, code in ((work / "output with spaces.o", 0), (self.project.root / "forbidden.o", 2)):
            with self.subTest(output=output):
                result = subprocess.run(["sh", str(script), str(source), "-o", str(output)], capture_output=True)
                self.assertEqual(result.returncode, code, result.stderr)
        self.assertFalse((self.project.root / "forbidden.o").exists())
