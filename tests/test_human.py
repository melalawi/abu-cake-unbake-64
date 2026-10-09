"""Plain findings, exact summaries, and terminal-only progress and labels."""
import io
import os
from dataclasses import replace

import pytest

from unbake import effort, human
from unbake.contracts import Finding, Origin, StageRecord


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(effort, "_listeners", [])
    monkeypatch.setattr(effort, "closed", lambda: ())
    monkeypatch.setattr(human, "_stream", None)
    monkeypatch.setattr(human, "_tty", False)
    monkeypatch.setattr(human, "_live", False)
    monkeypatch.setattr(human, "_last", "")
    monkeypatch.setattr(human.config, "sentence", lambda key: "plain sentence")


@pytest.mark.parametrize("blocking,prefix", [(True, "Refused"), (False, "Debt")])
def test_finding_nested_english(blocking, prefix):
    finding = Finding("effort.log", "the reason", "src/file.c", 7, "member", ("a", "b"),
                      ("bytes", "symbols"), blocking=blocking, action="unbake compare")
    assert human.finding(finding) == (
        f"{prefix}: plain sentence.\n  because the reason\n  in src/file.c:7\n"
        "  item member\n  versions a, b\n  missing bytes\n  missing symbols\n  next: unbake compare"
    )


@pytest.mark.parametrize("line", [0, -1, 2])
def test_finding_optional_fields(line):
    suffix = ":2" if line == 2 else ""
    assert human.finding(Finding("effort.log", "", "file", line)) == f"Refused: plain sentence.\n  in file{suffix}"
    assert human.finding(Finding("effort.log", "")) == "Refused: plain sentence."


def test_no_escape_codes_when_not_tty():
    stream = io.StringIO()
    human.attach(stream, False)
    effort.progress("work", 1, 2)
    human.finish("crack", {"member": "member", "label": "CRACKED"}, [Finding("effort.log", "reason")])
    assert "\x1b" not in stream.getvalue()
    assert stream.getvalue() == "Refused: plain sentence.\n  because reason\nmember: CRACKED\n"


@pytest.mark.parametrize("label,colour", [("CRACKED", "32"), ("NEEDS CREATIVE", "33"), ("OTHER", None)])
@pytest.mark.parametrize("feedback", [False, True])
def test_only_two_words_coloured_on_tty(label, colour, feedback):
    stream = io.StringIO()
    human.attach(stream, True)
    result = {"member": "member", "label": label}
    if feedback:
        result = {"feedback": {"member": "member", "label": label, "subsystem": "unknown",
                               "score_before": 0.2, "score_after": 0.75, "outcome": "better", "next": "edit source",
                               "hints": [{"id": "hint1", "technique": "try this", "because": {"score": 0.75}}]}}
    human.finish("compare", result, [Finding("effort.log", "reason")])
    text = stream.getvalue()
    expected = f"member: \x1b[{colour}m{label}\x1b[0m\n" if colour else ""
    assert text.startswith("Refused: plain sentence.\n  because reason\n" + expected)
    assert text.count("\x1b") == (2 if colour else 0)
    if feedback:
        assert text.endswith("member (unknown): 20.0% -> 75.0% (better)\n"
                             "  hint hint1: try this\n    because score=0.75\n  next: edit source\n")


@pytest.mark.parametrize("tty", [False, True])
def test_status_line_only_on_tty(monkeypatch, tty):
    stream = io.StringIO()
    monkeypatch.setattr(human.shutil, "get_terminal_size", lambda: os.terminal_size((12, 20)))
    human.attach(stream, tty)
    effort._notify("open", {"path": ["check", "longstage"]})
    effort.progress("units", 2, 4)
    effort._notify("close", {"path": ["check", "longstage"], "wall": 1, "status": "ok"})
    effort._notify("close", {"path": ["check"], "wall": 2, "status": "ok"})
    assert stream.getvalue() == ("\r\x1b[Kcheck > lon\r\x1b[Kunits 2/4\r\x1b[K" if tty else "")
    assert not human._live


def _records():
    keys = ("resources.memory_parent_bytes", "resources.memory_worker_bytes")
    root = StageRecord("inv", "root", None, ("check",), "command", "ok", "parallel", 0,
                       10, 2, 6, 12, 2, 4, 4, 2, 3, 2_000_000, 3_000_000,
                       {key: Origin(key, "/host.toml", "0" * 64) for key in keys},
                       dict(zip(keys, (100_000_000, 200_000_000), strict=True)),
                       {"view": (1, 2), "object": (3, 4)})
    stage = replace(root, span="stage", parent="root", path=("check", "work"), kind="stage", wall_seconds=5)
    own = replace(stage, span="own", path=("check", "(own)"), kind="own", wall_seconds=2,
                  findings=(Finding("budget.single_core", "too slow", blocking=False),))
    return [own, stage, root]


def test_summary_lines():
    assert human.summary(_records()) == [
        "took 10.00s, 2.00 cores (parent 2.00s, workers 6.00s, native 12.00s cpu)",
        "memory: parent peak 2.0 MB of 100.0 MB (/host.toml), workers peak 3.0 MB of 200.0 MB (/host.toml)",
        "cache: object 3 warm / 4 cold", "cache: view 1 warm / 2 cold", "slowest:",
        "  check  10.00s  2.00 cores  parallel  items 4 jobs 4 workers 2/3",
        "  check > work  5.00s  2.00 cores  parallel  items 4 jobs 4 workers 2/3",
        "  check > (own)  2.00s  2.00 cores  parallel  items 4 jobs 4 workers 2/3",
        "slow: plain sentence too slow",
    ]


def test_summary_last_root_and_ten_slowest():
    root = _records()[-1]
    records = [replace(root, span=str(i), kind="stage", wall_seconds=i, path=(str(i),)) for i in range(12)]
    records += [root, replace(root, wall_seconds=20)]
    lines = human.summary(records)
    assert lines[0].startswith("took 20.00s")
    assert len(lines[lines.index("slowest:") + 1:]) == 10
    assert human.summary([]) == []
    assert human.summary(records[:12]) == []


def test_finish_clears_and_orders_findings(monkeypatch):
    stream = io.StringIO()
    human.attach(stream, True)
    effort.progress("units", 1, 2)
    monkeypatch.setattr(effort, "closed", _records)
    human.finish("check", None, [Finding("effort.log", "debt", blocking=False), Finding("effort.log", "block")])
    text = stream.getvalue()
    assert text.startswith("\r\x1b[Kunits 1/2\r\x1b[KRefused: plain sentence.\n  because block\n"
                           "Debt: plain sentence.\n  because debt\n")
    assert "took 10.00s" in text
    assert not human._live
