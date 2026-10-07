"""Poison inherited temp paths while exercising file payloads and actual native/pool children."""

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import atomic, build, pool, process, sqlite
from unbake.config import Held
from unbake.typemap import shards, types_db

PAYLOAD = "transport payload " * 128
CHILD = """import json, os, tempfile
from pathlib import Path
with tempfile.NamedTemporaryFile() as stream:
    stream.write(b"native payload")
    stream.flush()
    print(json.dumps({"path": stream.name, "payload": Path(stream.name).read_text(),
                      "env": {key: os.environ[key] for key in ("TMPDIR", "TMP", "TEMP", "SQLITE_TMPDIR")}}))
"""


def worker_payload(shared, index):
    with tempfile.NamedTemporaryFile() as stream:
        stream.write(shared.encode())
        stream.flush()
        return str(Path(stream.name).parent), Path(stream.name).read_text(), index, os.environ["TMPDIR"]


class TempStorageTests(TempCase):
    def setUp(self):
        super().setUp()
        self.forbidden = self.root / "nonexistent-system-temp"
        self.poison = {key: str(self.forbidden) for key in ("TMPDIR", "TMP", "TEMP", "SQLITE_TMPDIR")}

    def test_native_child_and_atomic_command_use_explicit_storage(self):
        directory = self.root / "build"
        with patch.dict(os.environ, self.poison), patch.object(tempfile, "tempdir", str(self.forbidden)):
            result = process.run_native([sys.executable, "-c", CHILD], self.root, "compile", temporary_root=directory)
            row = json.loads(result.stdout)
            self.assertEqual(row["payload"], "native payload")
            self.assertEqual(Path(row["path"]).parent, directory)
            self.assertEqual(set(row["env"].values()), {str(directory)})
            target = directory / "output.txt"
            script = (
                CHILD.replace("    print(json.dumps(", "    result = json.dumps(").replace("}}))", "}})")
                + '\nPath(__import__("sys").argv[1]).write_text("atomic payload")\n'
            )
            # The command also creates a real temporary file before publishing its declared output.
            with patch.object(atomic.subprocess, "run", wraps=subprocess.run) as invoked:
                atomic.command([target], [sys.executable, "-c", script, str(target)])
            self.assertEqual(target.read_text(), "atomic payload")
            self.assertEqual(invoked.call_args.kwargs["env"]["TMPDIR"], str(directory))
        self.assertFalse(self.forbidden.exists())

    def test_make_environment_preserves_explicit_cache_and_clean_path(self):
        host = SimpleNamespace(cache_machine_root=self.root / "machine-cache", tool_path=(self.root / "tools",))
        with patch.dict(os.environ, self.poison):
            environment = build.environment(host)
        self.assertEqual(environment["TMPDIR"], str(host.cache_machine_root.resolve()))
        self.assertEqual(environment["SQLITE_TMPDIR"], environment["TMPDIR"])
        self.assertEqual(environment["PATH"], os.pathsep.join(map(str, host.tool_path)))
        self.assertFalse(self.forbidden.exists())

    def test_sqlite_payload_sort_and_shards_never_use_system_temp(self):
        with patch.dict(os.environ, self.poison), patch.object(tempfile, "tempdir", str(self.forbidden)):
            connection = sqlite.connect(self.root / "sort.sqlite")
            try:
                self.assertEqual(connection.execute("PRAGMA temp_store").fetchone(), (2,))
                connection.execute("CREATE TEMP TABLE payload (value TEXT)")
                connection.executemany("INSERT INTO payload VALUES (?)", [(f"{i:04d}:{PAYLOAD}",) for i in range(256)])
                rows = connection.execute("SELECT value FROM payload ORDER BY value DESC").fetchall()
                self.assertEqual(rows[0][0], f"0255:{PAYLOAD}")
            finally:
                connection.close()
            writer = shards.Writer(self.root)
            try:
                self.assertEqual(writer.connection.execute("PRAGMA temp_store").fetchone(), (2,))
                writer.add("unit", "us", {"payload": PAYLOAD})
                path = writer.finish()
            finally:
                writer.close()
            functions = shards.Functions(path, {"unit": {"aliases": [], "versions": {"us": {}}}})
            self.assertEqual(functions["unit"]["versions"]["us"]["payload"], PAYLOAD)
            self.assertEqual(functions.read(["unit"])["unit"], functions["unit"])
            with types_db._connection(path) as reader:
                self.assertEqual(reader.execute("PRAGMA temp_store").fetchone(), (2,))
        self.assertFalse(self.forbidden.exists())

    def test_real_pool_workers_transport_under_long_configured_cache(self):
        # Longer than sun_path: configured cache paths must still work without XDG_RUNTIME_DIR.
        directory = self.root / ("machine-cache-" + "x" * 90)
        with (
            patch.dict(os.environ, {**self.poison, "XDG_RUNTIME_DIR": str(self.forbidden)}),
            pool.Pool(2, 4 << 30, 1 << 30, 512 << 20, directory) as workers,
        ):
            rows = workers.run(worker_payload, [0, 1], PAYLOAD)
        self.assertEqual([row[2] for row in rows], [0, 1])
        for parent, payload, _, inherited in rows:
            self.assertEqual(Path(parent).parent, directory)
            self.assertEqual(inherited, parent)
            self.assertEqual(payload, PAYLOAD)
        self.assertFalse(list(directory.glob("shared-*")))
        self.assertFalse(self.forbidden.exists())

    def test_pool_without_configured_scratch_refuses_before_starting(self):
        with self.assertRaisesRegex(Held, "pool.scratch"):
            pool._executor(1, 512 << 20, 1)


class TempSourceGuardTests(TempCase):
    def test_grep_inventory_has_no_implicit_temp_or_child_environment(self):
        root = Path(__file__).resolve().parents[1]
        pattern = (
            r"tempfile|TemporaryDirectory|TemporaryFile|mkdtemp|mkstemp|mktemp|gettempdir|subprocess|sqlite3\.connect"
        )
        result = subprocess.run(
            ["rg", "-l", pattern, "src", "bin", "ci"],
            cwd=root,
            env=process.temporary_environment(self.root),
            capture_output=True,
            text=True,
            check=True,
        )
        failures = []
        factories = {
            "mkdtemp",
            "mkstemp",
            "mktemp",
            "TemporaryDirectory",
            "NamedTemporaryFile",
            "TemporaryFile",
            "SpooledTemporaryFile",
        }
        for name in result.stdout.splitlines():
            path = root / name
            if path.suffix != ".py":
                # Shell entrypoints currently have no temporary-file creation.
                self.assertNotIn("mktemp", path.read_text(), name)
                continue
            tree = ast.parse(path.read_text(), filename=name)
            aliases = {
                alias.asname or alias.name: alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "tempfile"
                for alias in node.names
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = ast.unparse(node.func)
                leaf = aliases.get(function, function.rsplit(".", 1)[-1])
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                explicit = {
                    key
                    for key, value in keywords.items()
                    if not (isinstance(value, ast.Constant) and value.value is None)
                }
                if leaf in factories and "dir" not in explicit:
                    failures.append(f"{name}:{node.lineno}: default temp: {function}")
                if leaf in {"gettempdir", "gettempdirb"}:
                    failures.append(f"{name}:{node.lineno}: system temp discovery")
                if (
                    function
                    in {
                        "subprocess.run",
                        "subprocess.Popen",
                        "subprocess.call",
                        "subprocess.check_call",
                        "subprocess.check_output",
                    }
                    and "env" not in explicit
                ):
                    failures.append(f"{name}:{node.lineno}: inherited child environment")
                if function == "sqlite3.connect" and name != "src/unbake/sqlite.py":
                    failures.append(f"{name}:{node.lineno}: SQLite connection bypasses temp policy")
        self.assertEqual(failures, [])
