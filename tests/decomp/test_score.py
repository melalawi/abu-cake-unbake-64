"""Real objdiff symbol scores, content cache reuse, and executable pinning."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.decomp import score
from unbake.project.config import Held


class ScoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        executable = self.root / "objdiff"
        executable.write_bytes(b"pinned objdiff fixture")
        external = SimpleNamespace(objdiff_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        self.policy = SimpleNamespace(
            objdiff_cli=executable, objdiff_sha256=external.objdiff_sha256, cache_root=self.root / "cache"
        )
        self.project = SimpleNamespace(version=self.version)
        self.target = self.root / "target.o"
        self.base = self.root / "base.o"
        self.object(self.target, 1)
        self.object(self.base, 1)
        mock = patch.object(score.subprocess, "run", side_effect=self.diff_output)
        mock.start()
        self.addCleanup(mock.stop)
        score.verified.clear()
        self.addCleanup(score.verified.clear)

    def version(self, name: str) -> SimpleNamespace:
        if name != "us":
            raise Held("config", f"version.{name} is missing")
        return SimpleNamespace(name=name)

    def object(self, path: Path, value: int) -> None:
        path.write_bytes(bytes([value]))

    def diff_output(self, command, **kwargs):
        self.assertEqual(command[:3], [str(self.policy.objdiff_cli), "diff", "-1"])
        self.assertIn("--format", command)
        self.assertEqual(command[command.index("--format") + 1], "json")
        document = {
            "left": {
                "symbols": [
                    {
                        "name": "sample",
                        "kind": "SYMBOL_FUNCTION",
                        "match_percent": 100.0 if self.target.read_bytes() == self.base.read_bytes() else 66.0,
                    }
                ]
            }
        }
        Path(command[command.index("--output") + 1]).write_text(json.dumps(document))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fuzzy(self, function: str = "sample") -> float:
        return score.fuzzy(self.project, self.policy, "us", function, self.target, self.base)

    def test_real_scores_and_cache_key_follow_bytes_not_paths(self) -> None:
        with patch.object(score.subprocess, "run", side_effect=self.diff_output) as execute:
            self.assertEqual(self.fuzzy(), 100.0)
            self.assertEqual(self.fuzzy(), 100.0)
            self.assertEqual(execute.call_count, 1)
            self.object(self.base, 2)
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
