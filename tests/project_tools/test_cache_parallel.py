"""Exercise cache publication through concurrent standalone compiler CLI readers."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered


class ParallelCacheTests(unittest.TestCase):
    def test_parallel_cli_readers_accept_publication_after_missing_stat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = Path.cwd()
            self.addCleanup(os.chdir, previous)
            self.addCleanup(patch.stopall)
            os.chdir(root)
            project, _ = fixture(root)
            write_rendered(project)
            hook = root / "hook"
            hook.mkdir()
            # Force the actual cross-process publication window, without changing
            # either the compiler CLI or cache publisher. The old two-stat get
            # sees missing then present and raises "expected cached file".
            (hook / "sitecustomize.py").write_text(
                "import json, os, re, subprocess\n"
                "from pathlib import Path\n"
                "original = Path.stat\n"
                "def stat(path, *args, **kwargs):\n"
                "    try: return original(path, *args, **kwargs)\n"
                "    except FileNotFoundError:\n"
                "        if os.environ.get('UNBAKE_STRESS_READER') and re.fullmatch('[0-9a-f]{64}', path.name):\n"
                "            env = dict(os.environ); env.pop('UNBAKE_STRESS_READER')\n"
                "            command = json.loads(env['UNBAKE_STRESS_WRITER'])\n"
                "            command += ['--output', 'writer-%s.o' % os.getpid()]\n"
                "            subprocess.run(command, env=env, check=True, capture_output=True)\n"
                "        raise\n"
                "Path.stat = stat\n"
            )
            command = [
                sys.executable,
                "tools/compile.py",
                "--kind",
                "cc",
                "--recipe",
                "tools/build.json",
                "--non-matching",
                "0",
                "--version",
                "us",
                "--unit",
                "src/middle.c",
                "--source",
                "src/middle.c",
                "--cache-root",
                str(root / "cache"),
            ]
            env = dict(os.environ)
            env.update(
                PYTHONPATH=str(hook) + os.pathsep + env.get("PYTHONPATH", ""),
                UNBAKE_STRESS_READER="1",
                UNBAKE_STRESS_WRITER=json.dumps(command),
            )
            graph = root / "stress.mk"
            graph.write_text(
                ".PHONY: all one two three four\nall: one two three four\n"
                "one two three four:\n\t" + shlex.join(command) + " --output $@.o\n"
            )
            cache_module = root / "tools/cache.py"
            fixed = cache_module.read_text()
            start = fixed.index("    def get(")
            end = fixed.index("    def _temporary(", start)
            old = (
                "    def get(self, kind, key):\n"
                "        path = self.path(kind, key)\n"
                "        if path.is_file(): return path\n"
                "        if path.exists(): raise Held('cache', f'{path}: expected cached file')\n"
                "        return None\n\n"
            )
            cache_module.write_text(fixed[:start] + old + fixed[end:])
            failed = subprocess.run(["make", "-j4", "-f", str(graph)], env=env, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertIn("expected cached file", failed.stderr)
            cache_module.write_text(fixed)
            for iteration in range(16):
                (root / "src/middle.c").write_text(f"cold input {iteration}")
                result = subprocess.run(["make", "-j4", "-f", str(graph)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                objects = [(root / f"{name}.o").read_bytes() for name in ("one", "two", "three", "four")]
                self.assertTrue(objects[0].startswith(b"\x7fELF"))
                self.assertEqual(len(set(objects)), 1)
            self.assertFalse(list((root / "cache").rglob(".pending-*")))
