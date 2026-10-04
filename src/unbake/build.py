"""check: plain `make check` in a clean environment (no Python), plus repository hygiene and C source rules."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from unbake import steps
from unbake.config import Held, Host, Project


@dataclass
class Outcome:
    ok: bool
    built: bool
    findings: list[str] = field(default_factory=list)
    make_tail: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def document(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "built": self.built,
            "findings": list(self.findings),
            "make_tail": list(self.make_tail),
            "seconds": round(self.seconds, 3),
        }

    def lines(self) -> list[str]:
        head = "check passed" if self.ok else "check failed"
        detail = (
            "every version byte-identical" if self.built and self.ok else ("build skipped" if not self.built else "")
        )
        return [f"{head}{': ' + detail if detail else ''} ({self.seconds:.1f}s)", *self.findings, *self.make_tail]


def source_findings(project: Project) -> list[str]:
    """C source rules: no inline asm, no unmarked fakematch patterns (decomp.checks)."""
    from unbake.decomp import checks

    findings = []
    for path in sorted(project.src.glob("*.c")):
        text = path.read_text()
        for finding in checks.run(text):
            if finding.fakematch is None:
                findings.append(f"{path.relative_to(project.root)}: {checks.message(finding)}")
    return findings


def environment(host: Host) -> dict[str, str]:
    """The only environment make sees: [tools].path, a HOME, the C locale."""
    directories = [str(directory) for directory in host.tool_path]
    return {"PATH": os.pathsep.join(directories), "HOME": os.environ["HOME"], "LC_ALL": "C"}


def python_visible(host: Host) -> str | None:
    probe = subprocess.run(
        ["/bin/sh", "-c", "command -v python3 || command -v python"],
        env=environment(host),
        capture_output=True,
        text=True,
    )
    return probe.stdout.strip() or None if probe.returncode == 0 else None


def make_command(host: Host, target: str) -> list[str]:
    return [
        str(host.make),
        f"-j{host.cores}",
        "--no-print-directory",
        target,
        f"CPP={host.cpp}",
        f"AS={host.mips_as}",
        f"LD={host.mips_ld}",
        f"N64LINK={host.n64link}",
    ]


def check(project: Project, host: Host, *, files_only: bool = False) -> Outcome:
    from unbake import buildfiles
    from unbake.project import hygiene

    started = time.monotonic()
    if files_only:
        drifted = [str(path.relative_to(project.root)) for path in buildfiles.drift(project, host)]
        findings = [f"{name}: differs from buildfiles output; run unbake recompute buildfiles" for name in drifted]
        findings += [*hygiene.tracked_findings(project, host), *source_findings(project)]
        return Outcome(not findings, False, findings, [], time.monotonic() - started)
    steps.ensure(project, host, ["buildfiles"])
    findings = [*hygiene.tracked_findings(project, host), *source_findings(project)]
    python = python_visible(host)
    if python is not None:
        raise Held("check", f"tools.path: {python} is visible; [tools].path must not contain Python")
    try:
        result = subprocess.run(
            make_command(host, "check"), cwd=project.root, env=environment(host), capture_output=True, text=True
        )
    except OSError as error:
        raise Held("check", f"tools.make: {host.make}: {error}") from error
    output = (result.stdout + result.stderr).splitlines()
    tail = output[-20:] if result.returncode else []
    ok = result.returncode == 0 and not findings
    return Outcome(ok, True, findings, tail, time.monotonic() - started)
