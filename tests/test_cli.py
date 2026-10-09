"""cli: handlers resolve from commands.toml, one stdout envelope, exit codes 0/1/2/3, human stderr."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import fixture
import pytest

from unbake import cli, config, repo
from unbake.contracts import Finding, Refusal

Capture = pytest.CaptureFixture[str]
Patch = pytest.MonkeyPatch
COMMANDS = config.load_resource("commands.toml")["command"]


def _envelope(out: str) -> dict[str, Any]:
    lines = out.splitlines()
    assert len(lines) == 1
    document = json.loads(lines[0])
    config.validate("envelope", document, "envelope")
    return document


@pytest.fixture
def argv(tmp_path: Path, toolchains: dict[str, Any], monkeypatch: Patch) -> list[str]:
    monkeypatch.delenv("UNBAKE_HOST", raising=False)
    project = fixture.project(tmp_path, functions={"func_80000400": {"a": (0x1000, bytes(8)), "b": (0x1000, bytes(8))}})
    return ["setup", "--host", str(fixture.host(tmp_path)), "--project", str(project)]


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_every_command_has_handler(name: str) -> None:
    module, function = COMMANDS[name]["handler"].split(".")
    assert callable(getattr(import_module(f"unbake.{module}"), function))


def test_stdout_one_envelope(argv: list[str], monkeypatch: Patch, capsys: Capture) -> None:
    monkeypatch.setattr(repo, "setup", lambda cfg, params: {})
    assert cli.main(argv) == 0
    document = _envelope(capsys.readouterr().out)
    assert document["ok"] is True
    assert document["result"] == {}


def test_refusal_exit_1_and_human_stderr(argv: list[str], monkeypatch: Patch, capsys: Capture) -> None:
    def refuse(cfg: Any, params: Any) -> None:
        raise Refusal(Finding("config.missing", "no value"))

    monkeypatch.setattr(repo, "setup", refuse)
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert "Refused:" in captured.err
    document = _envelope(captured.out)
    assert document["ok"] is False
    assert document["findings"][0]["key"] == "config.missing"


def test_usage_exit_2(capsys: Capture) -> None:
    assert cli.main(["no-such-command"]) == 2
    assert capsys.readouterr().err.startswith("usage:")


def test_internal_exit_3(argv: list[str], monkeypatch: Patch, capsys: Capture) -> None:
    def explode(cfg: Any, params: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(repo, "setup", explode)
    assert cli.main(argv) == 3
    captured = capsys.readouterr()
    assert "Traceback" in captured.err
    document = _envelope(captured.out)
    assert document["findings"][0]["key"] == "internal.error"


def test_refused_landing_entries_fail_the_command_with_their_findings(argv: list[str], monkeypatch: Patch,
                                                                      capsys: Capture) -> None:
    refused = {"id": "x", "member": "f", "findings": [
        Finding("land.not_exact", "f differs", unit="f", versions=("a",), missing=("size",)).__dict__]}
    drain = {"running": False, "landed": [], "refused": [refused]}
    monkeypatch.setattr(repo, "setup", lambda cfg, params: {"drain": drain})
    assert cli.main(argv) == 1
    document = _envelope(capsys.readouterr().out)
    assert document["ok"] is False
    assert [(f["key"], f["unit"]) for f in document["findings"]] == [("land.not_exact", "f")]
    assert document["result"]["drain"]["refused"]


def test_a_command_that_drained_nothing_succeeds(argv: list[str], monkeypatch: Patch, capsys: Capture) -> None:
    monkeypatch.setattr(repo, "setup", lambda cfg, params: {"drain": None, "exact": 3})
    assert cli.main(argv) == 0
    assert _envelope(capsys.readouterr().out)["ok"] is True


def test_a_failed_exit_without_findings_names_itself(capsys: Capture) -> None:
    cli.human.attach(sys.stderr, False)
    assert cli._emit("submit", 1, None, []) == 1
    document = _envelope(capsys.readouterr().out)
    assert document["ok"] is False
    assert [f["key"] for f in document["findings"]] == ["internal.no-cause"]
    assert "submit exited 1" in document["findings"][0]["reason"]


def test_missing_host_refuses_config_missing(tmp_path: Path, monkeypatch: Patch, capsys: Capture) -> None:
    monkeypatch.delenv("UNBAKE_HOST", raising=False)
    assert cli.main(["setup", "--project", str(tmp_path)]) == 1
    document = _envelope(capsys.readouterr().out)
    assert document["findings"][0]["key"] == "config.missing"

def test_global_host_and_project(argv: list[str], monkeypatch: Patch, capsys: Capture) -> None:
    monkeypatch.setattr(repo, "setup", lambda cfg, params: {"workers": cfg.host.workers})
    assert cli.main([*argv[1:], argv[0]]) == 0
    assert _envelope(capsys.readouterr().out)["result"]["workers"] > 0


def test_command_host_overrides_global(argv: list[str], monkeypatch: Patch, capsys: Capture) -> None:
    monkeypatch.setattr(repo, "setup", lambda cfg, params: {})
    assert cli.main(["--host", "/not/configured", *argv]) == 0
    assert _envelope(capsys.readouterr().out)["ok"]
