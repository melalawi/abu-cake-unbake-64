"""Journal filesystem tests; Git is mocked so this lane never starts Git."""

from __future__ import annotations

import base64
import fcntl
import json
import os
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import fixture
import pytest

from unbake import journal
from unbake.contracts import Config, Finding, Host, Plan, Project, Refusal, digest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    project = Project(tmp_path, "test", "test", "Test", (), "", "test", {}, {}, {}, {}, 200, {}, "project")
    host = Host(1, 1, 1024, 1024, 1024, tmp_path / "tools", {}, None,
                2, 2, 0.8, 2, ("Writer", "writer@example.invalid"), {}, "host",
                    budget_dir=tmp_path / "budget")
    config = Config(project, host, "config")
    state = {"head": "a" * 40, "status": b"", "log": b"", "committed": {}, "paths": (), "tracked": (), "blobs": {}}

    def git(config_arg, *args):
        assert config_arg is config
        if args == ("rev-parse", "HEAD"):
            return fixture.native_result(stdout=(state["head"] + "\n").encode())
        if "status" in args:
            assert args[:5] == ("--literal-pathspecs", "status", "--porcelain", "-z", "--")
            return fixture.native_result(stdout=state["status"])
        if "add" in args:
            assert args[:4] == ("--literal-pathspecs", "add", "-A", "--")
            state["paths"] = args[4:]
            return fixture.native_result()
        if "commit" in args:
            assert args[:4] == ("-c", "user.name=Writer", "-c", "user.email=writer@example.invalid")
            state["manifest"] = json.loads((tmp_path / ".unbake/journal.json").read_bytes())
            state["committed"] = {
                path: (tmp_path / path).read_bytes() if (tmp_path / path).exists() else None
                for path in state["paths"]
            }
            state["head"] = "b" * 40
            state["log"] = (args[6] + "\n\n" + args[8] + "\n").encode()
            return fixture.native_result()
        if "ls-files" in args:
            assert args[:4] == ("--literal-pathspecs", "ls-files", "-z", "--")
            return fixture.native_result(stdout=b"".join(p.encode() + b"\0" for p in state["tracked"] if p in args[4:]))
        if args[0] == "show":
            return fixture.native_result(stdout=state["blobs"][args[1].split(":", 1)[1]])
        if args == ("log", "-1", "--format=%B"):
            return fixture.native_result(stdout=state["log"])
        raise AssertionError(f"unexpected Git arguments: {args!r}")

    git_mock = Mock(side_effect=git)
    monkeypatch.setattr(journal.process, "git", git_mock)
    stage = Mock(side_effect=lambda name: nullcontext())
    monkeypatch.setattr(journal.effort, "stage", stage)
    return config, state, git_mock, stage


def _plan(writes, **changes):
    return replace(Plan("repair", "snapshot", writes, (), (), (), "Install exact bytes", digest(writes)), **changes)


def _seed(config, files):
    for name, content in files.items():
        path = config.project.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _manifest(config, head, plan, before):
    path = config.project.root / ".unbake/journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"head": head, "plan": plan, "before": {
        name: base64.b64encode(content).decode() if content is not None else None
        for name, content in before.items()
    }}))
    return path


def _refused(error, key, missing=None):
    assert len(error.value.findings) == 1
    finding = error.value.findings[0]
    assert finding.key == key
    if missing is not None:
        assert finding.missing == missing


def test_apply_commits_exact_bytes(repo):
    config, state, git, stage = repo
    old = {"a.bin": b"old\x00\xff", "deleted.bin": b"delete", "unrelated.bin": b"keep"}
    _seed(config, old)
    writes = {"new/sub.bin": b"\x00\xff\r\n", "deleted.bin": None, "a.bin": b"new\x80"}
    plan = _plan(writes)
    head = state["head"]
    new = journal.apply(config, plan, head)
    assert new == state["head"] == "b" * 40
    assert state["committed"] == writes
    assert state["paths"] == tuple(sorted(writes))
    assert state["manifest"] == {"head": head, "plan": plan.digest, "before": {
        "a.bin": base64.b64encode(old["a.bin"]).decode(),
        "deleted.bin": base64.b64encode(old["deleted.bin"]).decode(), "new/sub.bin": None,
    }}
    assert state["log"] == f"{plan.message}\n\nplan {plan.digest}\n".encode()
    assert (config.project.root / "unrelated.bin").read_bytes() == b"keep"
    assert not (config.project.root / ".unbake/journal.json").exists()
    assert [call.args[1:] for call in git.call_args_list if "status" in call.args] == [
        ("--literal-pathspecs", "status", "--porcelain", "-z", "--", *sorted(writes)),
    ] * 2
    stage.assert_called_once_with("journal.apply")


def test_head_moved_refused(repo):
    config, state, git, _ = repo
    _seed(config, {"a": b"original"})
    stale = "c" * 40
    with pytest.raises(Refusal) as error:
        journal.apply(config, _plan({"a": b"new", "new": b"created"}), stale)
    _refused(error, "journal.changed", (f"HEAD moved from {stale} to {state['head']}",))
    assert (config.project.root / "a").read_bytes() == b"original"
    assert not (config.project.root / "new").exists()
    assert not (config.project.root / ".unbake/journal.json").exists()
    assert not any("add" in call.args or "commit" in call.args for call in git.call_args_list)


@pytest.mark.parametrize("status,missing", [
    (b" M planned\0", ("planned",)),
    (b"M  planned\0", ("planned",)),
    (b"?? planned\0", ("planned",)),
    (b" D planned\0", ("planned",)),
    (b"R  planned\0old name\0", ("planned", "old name")),
    (b"C  planned\0old name\0", ("planned", "old name")),
    (" M space \"雪\"\nfile\0".encode(), ('space "雪"\nfile',)),
])
def test_dirty_path_refused(repo, status, missing):
    config, state, git, _ = repo
    _seed(config, {"planned": b"dirty"})
    state["status"] = status
    with pytest.raises(Refusal) as error:
        journal.apply(config, _plan({"planned": b"new"}), state["head"])
    _refused(error, "journal.changed", missing)
    assert "commit or revert" in error.value.findings[0].action
    assert (config.project.root / "planned").read_bytes() == b"dirty"
    assert not (config.project.root / ".unbake/journal.json").exists()
    assert not any("commit" in call.args for call in git.call_args_list)


def test_interrupt_then_recover(repo):
    config, state, git, stage = repo
    before = {"a": b"original\x00\xff", "deleted": b"restore"}
    _seed(config, before)
    original_git = git.side_effect

    def interrupt(config_arg, *args):
        if "commit" in args:
            raise InterruptedError("commit interrupted")
        return original_git(config_arg, *args)

    git.side_effect = interrupt
    with pytest.raises(InterruptedError, match="commit interrupted"):
        journal.apply(config, _plan({"a": b"changed", "deleted": None, "new/nested": b"new"}), state["head"])
    assert (config.project.root / "a").read_bytes() == b"changed"
    assert not (config.project.root / "deleted").exists()
    assert (config.project.root / "new/nested").read_bytes() == b"new"
    assert (config.project.root / ".unbake/journal.json").exists()
    assert journal.recover(config) is True
    for path, content in before.items():
        assert (config.project.root / path).read_bytes() == content
    assert not (config.project.root / "new/nested").exists()
    assert not (config.project.root / ".unbake/journal.json").exists()
    assert state["head"] == "a" * 40
    assert [call.args[0] for call in stage.call_args_list] == ["journal.apply", "journal.recover"]


def test_a_tracked_file_is_recorded_by_name_and_restored_from_git(repo):
    config, state, git, _ = repo
    _seed(config, {"big": b"old bytes", "loose": b"untracked"})
    state["tracked"], state["blobs"] = ("big",), {"big": b"old bytes"}
    original_git = git.side_effect

    def interrupt(config_arg, *args):
        if "commit" in args:
            raise InterruptedError("commit interrupted")
        return original_git(config_arg, *args)

    git.side_effect = interrupt
    with pytest.raises(InterruptedError):
        journal.apply(config, _plan({"big": b"new", "loose": b"new"}), state["head"])
    manifest = json.loads((config.project.root / ".unbake/journal.json").read_bytes())
    assert manifest["before"] == {"big": "git", "loose": base64.b64encode(b"untracked").decode()}
    assert journal.recover(config) is True
    assert (config.project.root / "big").read_bytes() == b"old bytes"
    assert (config.project.root / "loose").read_bytes() == b"untracked"


def test_no_partial_install(repo, monkeypatch):
    config, state, _, _ = repo
    before = {"a": b"old a", "b": b"old b"}
    _seed(config, before)
    original_replace = os.replace
    installed = []

    def fail_second(source, target):
        target = Path(target)
        if target.parent == config.project.root:
            installed.append(target.name)
            if len(installed) == 2:
                raise OSError("second replacement failed")
        return original_replace(source, target)

    monkeypatch.setattr(journal.os, "replace", fail_second)
    with pytest.raises(OSError, match="second replacement failed"):
        journal.apply(config, _plan({"b": b"new b", "a": b"new a"}), state["head"])
    assert installed == ["a", "b"]
    assert (config.project.root / "a").read_bytes() == b"new a"
    assert (config.project.root / "b").read_bytes() == b"old b"
    assert journal.recover(config) is True
    assert {name: (config.project.root / name).read_bytes() for name in before} == before
    assert not (config.project.root / ".unbake/journal.json").exists()
    assert not list(config.project.root.rglob(".unbake-*"))


@pytest.mark.parametrize("mode", ["bytes", "deleted_reappears", "missing", "status"])
def test_readback_refused_keeps_committed_manifest(repo, mode):
    config, state, git, _ = repo
    _seed(config, {"a": b"original"})
    plan = _plan({"a": None if mode == "deleted_reappears" else b"committed"})
    original_git = git.side_effect

    def corrupt_after_commit(config_arg, *args):
        result = original_git(config_arg, *args)
        if "commit" in args:
            if mode == "missing":
                (config.project.root / "a").unlink()
            elif mode == "status":
                state["status"] = b" M a\0"
            else:
                (config.project.root / "a").write_bytes(b"corrupt")
        return result

    git.side_effect = corrupt_after_commit
    with pytest.raises(Refusal) as error:
        journal.apply(config, plan, state["head"])
    _refused(error, "journal.readback", ("a",))
    assert (config.project.root / ".unbake/journal.json").exists()
    assert state["head"] == "b" * 40
    assert journal.recover(config) is False
    assert not (config.project.root / ".unbake/journal.json").exists()
    assert state["committed"] == plan.writes
    assert not any("reset" in call.args or "--amend" in call.args for call in git.call_args_list)


@pytest.mark.parametrize("log,accepted", [
    (b"subject\n\nplan expected\n", True),
    (b"subject\n\nplan other\n", False),
    (b"subject\n\nplan expected-extra\n", False),
])
def test_recover_moved_head_checks_digest(repo, log, accepted):
    config, state, _, _ = repo
    _seed(config, {"a": b"committed"})
    manifest = _manifest(config, "old-head", "expected", {"a": b"original"})
    state["log"] = log
    if accepted:
        assert journal.recover(config) is False
        assert not manifest.exists()
    else:
        with pytest.raises(Refusal) as error:
            journal.recover(config)
        _refused(error, "journal.changed")
        assert manifest.exists()
    assert (config.project.root / "a").read_bytes() == b"committed"


def test_recover_without_manifest(repo):
    config, _, git, stage = repo
    assert journal.recover(config) is False
    git.assert_not_called()
    stage.assert_called_once_with("journal.recover")


def test_blocking_plan_is_programming_error(repo):
    config, state, git, _ = repo
    plan = _plan({"a": b"new"}, blocking=(Finding("journal.changed", "blocked"),))
    with pytest.raises(ValueError, match="blocking plan"):
        journal.apply(config, plan, state["head"])
    git.assert_not_called()
    assert not (config.project.root / "a").exists()


def test_apply_holds_blocking_lock_through_git(repo, monkeypatch):
    config, state, git, _ = repo
    real_flock = fcntl.flock
    lock_calls = []

    def flock(fd, operation):
        lock_calls.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(journal.store.fcntl, "flock", flock)
    original_git = git.side_effect

    def check_lock(config_arg, *args):
        with (
            (config.project.root / ".unbake/journal.lock").open("a+b") as other,
            pytest.raises(BlockingIOError),
        ):
            real_flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_git(config_arg, *args)

    git.side_effect = check_lock
    journal.apply(config, _plan({"a": b"new"}), state["head"])
    assert lock_calls == [fcntl.LOCK_EX | fcntl.LOCK_NB]
    with (config.project.root / ".unbake/journal.lock").open("a+b") as other:
        real_flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("content", [b"", b"\x00\xff\n", None])
def test_create_empty_binary_or_delete_absent(repo, content):
    config, state, _, _ = repo
    name = "nested/space [literal] 雪"
    journal.apply(config, _plan({name: content}), state["head"])
    path = config.project.root / name
    if content is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == content
    assert state["committed"] == {name: content}
