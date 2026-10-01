"""Real objdiff symbol scores, content cache reuse, and executable pinning."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import test_policy, tool
from unbake.decomp import score
from unbake.project.config import Held


class ScoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        external = test_policy(self.root)
        executable = external.objdiff_cli
        self.policy = SimpleNamespace(
            objdiff_cli=executable, objdiff_sha256=external.objdiff_sha256, cache_root=self.root / "cache"
        )
        self.project = SimpleNamespace(version=self.version)
        self.target = self.root / "target.o"
        self.base = self.root / "base.o"
        self.object(self.target, 1)
        self.object(self.base, 1)
        score.verified.clear()
        self.addCleanup(score.verified.clear)

    def version(self, name: str) -> SimpleNamespace:
        if name != "us":
            raise Held("config", f"version.{name} is missing")
        return SimpleNamespace(name=name)

    def object(self, path: Path, value: int) -> None:
        assembler = tool("mips-linux-gnu-as")
        body = (
            ".text\n.set noreorder\n.globl sample\n.type sample,@function\nsample:\n"
            f"addiu $v0,$zero,{value}\njr $ra\nnop\n.size sample,.-sample\n"
        )
        subprocess.run(
            [assembler, "-EB", "-mips3", "-o", str(path)], input=body, capture_output=True, text=True, check=True
        )

    def fuzzy(self, function: str = "sample") -> float:
        return score.fuzzy(self.project, self.policy, "us", function, self.target, self.base)

    def test_real_scores_and_cache_key_follow_bytes_not_paths(self) -> None:
        with patch.object(score.subprocess, "run", wraps=subprocess.run) as execute:
            self.assertEqual(self.fuzzy(), 100.0)
            self.assertEqual(self.fuzzy(), 100.0)
            self.assertEqual(execute.call_count, 1)
            self.object(self.base, 2)
            # The independent assembler call also passes through subprocess.run.
            before = execute.call_count
            value = self.fuzzy()
            self.assertGreater(value, 0)
            self.assertLess(value, 100)
            self.assertEqual(execute.call_count, before + 1)
        self.assertEqual(len(list((self.policy.cache_root / "score").glob("*/*"))), 2)

    def test_binary_digest_is_verified_once_per_process_pair(self) -> None:
        with patch.object(score.hashlib, "file_digest", wraps=hashlib.file_digest) as digest:
            self.assertEqual(score.objdiff_cli(self.policy), self.policy.objdiff_cli.resolve())
            self.assertEqual(score.objdiff_cli(self.policy), self.policy.objdiff_cli.resolve())
            self.assertEqual(digest.call_count, 1)
        self.policy.objdiff_sha256 = "0" * 64
        with self.assertRaisesRegex(Held, "objdiff_sha256"):
            score.objdiff_cli(self.policy)

    def test_missing_symbol_and_object_are_refused(self) -> None:
        with self.assertRaisesRegex(Held, "missing_function"):
            self.fuzzy("missing_function")
        self.base.unlink()
        with self.assertRaisesRegex(Held, "base_obj"):
            self.fuzzy()

    def test_native_json_omitted_zero_score_and_invalid_schema(self) -> None:
        def run_with(document: object) -> Callable[..., SimpleNamespace]:
            def execute(command: list[str], **kwargs: object) -> SimpleNamespace:
                Path(command[command.index("--output") + 1]).write_text(json.dumps(document))
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            return execute

        with patch.object(
            score.subprocess,
            "run",
            side_effect=run_with({"left": {"symbols": [{"name": "sample", "kind": "SYMBOL_FUNCTION"}]}}),
        ):
            self.assertEqual(self.fuzzy(), 0.0)
        shutil.rmtree(self.policy.cache_root)
        with (
            patch.object(score.subprocess, "run", side_effect=run_with({"left": {}})),
            self.assertRaisesRegex(Held, "left.symbols"),
        ):
            self.fuzzy()
        self.assertEqual(list((self.policy.cache_root / "score").glob("*/*")), [])

    def test_command_failure_does_not_cache_output(self) -> None:
        with (
            patch.object(
                score.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="invalid MIPS object"),
            ),
            self.assertRaisesRegex(Held, "invalid MIPS object"),
        ):
            self.fuzzy()
        self.assertEqual(list((self.policy.cache_root / "score").glob("*/*")), [])

    def test_weakest_refuses_empty_or_invalid_percentages(self) -> None:
        self.assertEqual(score.weakest({"us": 99, "eu": 85.5}), 85.5)
        for scores in ({}, {"us": float("nan")}, {"us": -1}, {"us": 101}, {"us": True}):
            with self.subTest(scores=scores), self.assertRaises(Held):
                score.weakest(scores)
