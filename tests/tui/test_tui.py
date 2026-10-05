"""The human output: plain lines with a throttle, nesting, two verdict labels, one choice per process."""

import io
import os
import re
import unittest
from unittest.mock import patch

from unbake import effort, tui
from unbake.config import Held
from unbake.tui import progress, render

STAMP = r"\d\d:\d\d:\d\d  "


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PlainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.clock = Clock()
        progress.bind(render.Plain(self.stream, clock=self.clock, wall=self.clock, color=False))
        self.addCleanup(progress.bind, None)

    def lines(self) -> list[str]:
        return self.stream.getvalue().splitlines()

    def test_start_progress_throttle_and_done_line(self) -> None:
        spent = effort.Effort(wall=252.0, main=252.0, tools=0.0, pool={"x": (504.0, 3)}, counts={"cache.kind": (3, 4)})
        with patch.object(effort, "since", return_value=spent), tui.task("Reading the C files", 10) as task:
            task.advance()
            self.clock.now += 9
            task.advance()  # inside ten seconds: silent
            self.clock.now += 2
            task.advance()
        lines = self.lines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], STAMP + "Reading the C files$")
        self.assertRegex(lines[1], STAMP + r"Reading the C files: 3 of 10 \(30%\)$")
        self.assertRegex(
            lines[2], STAMP + r"Reading the C files: done in 4m 12s, 3\.0 cores; 3 of 4 reused from the cache$"
        )

    def test_durations(self) -> None:
        self.assertEqual([render.duration(s) for s in (42, 252, 3780)], ["42s", "4m 12s", "1h 3m"])

    def test_nested_tasks_and_lines_indent_two_spaces_per_depth(self) -> None:
        with tui.task("outer"), tui.task("inner"):
            tui.line("a sentence")
        text = self.lines()
        self.assertRegex(text[0], STAMP + "outer$")
        self.assertRegex(text[1], STAMP + "  inner$")
        self.assertRegex(text[2], STAMP + "    a sentence$")

    def test_verdict_colour_only_when_forced(self) -> None:
        for forced, expected in ((False, "NEEDS CREATIVE  f: x"), (True, "\x1b[1;33mNEEDS CREATIVE  f: x\x1b[0m")):
            with self.subTest(forced=forced):
                self.stream.truncate(0)
                self.stream.seek(0)
                with patch.dict(os.environ, {"FORCE_COLOR": "1"} if forced else {}, clear=not forced):
                    progress.bind(render.Plain(self.stream, clock=self.clock, wall=self.clock))
                    tui.verdict("creative", "f: x")
                self.assertTrue(self.stream.getvalue().rstrip("\n").endswith(expected), self.stream.getvalue())
        self.assertIn("CRACKED  f: y", self._cracked())

    def _cracked(self) -> str:
        self.stream.truncate(0)
        self.stream.seek(0)
        progress.bind(render.Plain(self.stream, clock=self.clock, wall=self.clock, color=False))
        tui.verdict("cracked", "f: y")
        return self.stream.getvalue()


class StartTests(unittest.TestCase):
    def test_start_twice_raises(self) -> None:
        self.addCleanup(tui.stop)
        tui.start(io.StringIO(), False)
        with self.assertRaises(Held) as raised:
            tui.start(io.StringIO(), False)
        self.assertEqual(raised.exception.reason, "tui.start: called twice")

    def test_lines_without_a_start_go_to_the_error_stream_plain(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            tui.line("hello")
        self.assertEqual(err.getvalue(), "hello\n")
        self.assertIsNone(re.match(STAMP, err.getvalue()))


if __name__ == "__main__":
    unittest.main()
