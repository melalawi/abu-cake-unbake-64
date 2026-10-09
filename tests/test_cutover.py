"""Throwaway migration readback rules, using temporary files and no subprocesses."""
import importlib.util
import tomllib
from argparse import Namespace
from pathlib import Path

import pytest
import tomlkit

from unbake import config

SPEC = importlib.util.spec_from_file_location(
    "migrate_cutover", Path(__file__).resolve().parents[1] / "cutover/migrate_cutover.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def test_host_omits_unsupplied_tools(tmp_path):
    host = {"resources": {"memory_parent_bytes": 1024, "memory_worker_bytes": 1024},
            "cache": {"machine_root": str(tmp_path / "cache"), "max_bytes": 1024},
            "tools": {"make": "/usr/bin/make", "splat": "/usr/bin/true"},
            "publish": {"author_name": "test", "author_email": "test@example.com"}}
    old = tmp_path / "old.toml"
    old.write_text(tomlkit.dumps(host))
    args = Namespace(host=str(old), out_host=str(tmp_path / "new.toml"),
                     toolchain_root=str(tmp_path / "tc"), git="/usr/bin/git", report={})
    actions = migration.host_actions(args)
    migration.apply(actions)
    assert set(config.load_host(Path(args.out_host)).tools) == {"git", "make", "splat"}


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("grouped", [True, False])
def test_published_project_c_members_and_flags_readback(tmp_path, schema, grouped):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/f.c").write_text("int f(void) { return 1; }")
    (tmp_path / "split.yaml").write_text(
        "options:\n  create_c_files: True\nsegments:\n  - subsegments:\n      - [0, c, f]\n")
    (tmp_path / "layout.toml").write_text(tomlkit.dumps({"schema": 1, "cap": 32, "group": [
        {"name": "group-f", "segment": "main", "members": ["f"], "evidence": "proven"}] if grouped else []}))
    (tmp_path / "README.md").write_text("Intro\n## Progress\n<pre>test</pre>\n")
    old = {"schema": schema, "project": {"id": "fixture", "state": "ready", "name": "Fixture",
           "title": "Fixture", "versions": ["a"], "names_from": "a", "default_compiler": "gcc-test", "layout_cap": 32},
           "build": {"cppflags": [], "asflags": [], "sn64_asflags" if schema == 1 else "gnu_asflags": ["-mips3"]},
           "version": {"a": {"baserom": "roms/base.z64", "baserom_sha1": "0" * 40,
                        "split": "split.yaml", "symbols": "symbols.txt", "macros": ["-DVERSION_A"]}},
           "compilers": {"gcc-test": {"cflags": ["-O2", "-mfp64", "-fstrength-reduce", "-fthread-jumps"]}},
           "units": {"f" if schema == 1 else "src/f.c": {"compiler": "gcc-test",
                     "flags" if schema == 1 else "options": ["-mfp32", "-fno-strength-reduce"] if schema == 1
                     else {"compile": ["-mfp32", "-fno-strength-reduce"]}}}}
    (tmp_path / "config.toml").write_text(tomlkit.dumps(old))
    report = {}
    migration.PRE.clear()
    migration.TRACKED.clear()
    migration.apply(migration.project_actions(tmp_path, report))
    new = tomllib.loads((tmp_path / "config.toml").read_text())
    layout = tomllib.loads((tmp_path / "layout.toml").read_text())
    assert new["schema"] == 3 and new["build"]["gnu_asflags"] == ["-mips3"]
    assert report["c_members"] == [{"member": "f", "version": "a"}]
    unit, = layout["unit"]
    assert unit["group"] == ("group-f" if grouped else "unit_f")
    assert unit["path"] == "src/f.c" and unit["options"]["omit"] == ["-mfp64", "-fstrength-reduce"]
    assert "-fthread-jumps" not in unit["options"]["omit"]
    config.validate("project", new, "config.toml")
    config.validate("layout", layout, "layout.toml")
    text = (tmp_path / "README.md").read_text()
    assert (text.replace(migration.OPEN + "\n", "").replace(migration.CLOSE + "\n", "").encode()
            == migration.PRE["README.md"])


def test_conflicting_schema1_assembler_alias_refused(tmp_path):
    with pytest.raises(SystemExit, match="conflicts"):
        migration.legacy_project(tmp_path, {"build": {"sn64_asflags": ["a"], "gnu_asflags": ["b"]}})

def test_commit_paths_use_nul_stdin(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(migration, "run", lambda argv, cwd, **kw: calls.append((argv, kw)))
    migration.TRACKED.clear()
    paths = [tmp_path / f"src/member-{i}.c" for i in range(10000)]
    migration.commit(tmp_path, [("write", p, b"") for p in paths])
    argv, kw = calls[0]
    assert argv == ["git", "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"]
    assert set(kw["input"].split("\0")[:-1]) == {str(p.relative_to(tmp_path)) for p in paths}
    assert calls[1][0] == ["git", "commit", "-m", "migrate to unbake v2"]


def test_leftovers_are_listed_deleted_in_one_commit_and_read_back(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout
    git("init", "-q")
    for name in ("memo.sh", "Makefile"):
        (tmp_path / name).write_text(name)
    git("add", "-A")
    git("commit", "-qm", "before")
    migration.TRACKED.clear()
    actions = migration.leftover_actions(tmp_path)  # the preflight
    assert [a[1].name for a in actions] == ["memo.sh"] and (tmp_path / "memo.sh").exists()
    migration.apply(actions)
    migration.commit(tmp_path, actions, "migrate: remove files earlier unbake versions generated")
    assert not (tmp_path / "memo.sh").exists() and (tmp_path / "Makefile").exists()
    assert git("ls-files").split() == ["Makefile"] and git("status", "--porcelain") == ""
    migration.TRACKED.clear()
    assert migration.leftover_actions(tmp_path) == []  # nothing to do twice
