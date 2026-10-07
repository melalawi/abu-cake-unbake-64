"""check: plain `make check` in a clean environment (no Python), plus repository hygiene and C source rules."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from unbake import process, steps
from unbake.config import Held, Host, Project
from unbake.process import capture
from unbake.process import named as cause_named


@dataclass
class Outcome:
    ok: bool
    built: bool
    findings: list[str] = field(default_factory=list)
    make_tail: list[str] = field(default_factory=list)
    seconds: float = 0.0
    fault: dict[str, Any] | None = None
    preflight: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "built": self.built,
            "findings": list(self.findings),
            "make_tail": list(self.make_tail),
            "seconds": round(self.seconds, 3),
            **self.preflight,
            **({"fault": self.fault} if self.fault is not None else {}),
        }

    def lines(self) -> list[str]:
        head = "check passed" if self.ok else "check failed"
        detail = (
            "every version byte-identical" if self.built and self.ok else ("build skipped" if not self.built else "")
        )
        return [f"{head}{': ' + detail if detail else ''} ({self.seconds:.1f}s)", *self.findings, *self.make_tail]


def environment(host: Host) -> dict[str, str]:
    """The only environment make sees: [tools].path, a HOME, the C locale."""
    directories = [str(directory) for directory in host.tool_path]
    return process.temporary_environment(
        host.cache_machine_root, {"PATH": os.pathsep.join(directories), "HOME": os.environ["HOME"], "LC_ALL": "C"}
    )


def python_visible(host: Host) -> str | None:
    probe = subprocess.run(
        ["/bin/sh", "-c", "command -v python3 || command -v python"],
        env=environment(host),
        capture_output=True,
        text=True,
    )
    return probe.stdout.strip() or None if probe.returncode == 0 else None


def make_command(host: Host, target: str) -> list[str]:
    """Use the command's configured cores in standalone and broker modes alike."""
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
    try:
        prepared = steps.prepare(project, host, steps.PrepareRequest("check", (), project_scope=True))
    except Held as error:
        if error.key != "check.source_rules":
            raise
        return Outcome(
            False,
            False,
            [error.reason],
            seconds=time.monotonic() - started,
            fault=error.fault.document(),
            preflight={**error.data, "key": error.key, "action": error.fault.cause.action},
        )
    findings = hygiene.tracked_findings(project, host)
    if files_only:
        drifted = [str(path.relative_to(project.root)) for path in buildfiles.drift(project, host)]
        findings += [f"{name}: differs from buildfiles output; run unbake recompute buildfiles" for name in drifted]
    if findings or files_only:
        return Outcome(not findings, False, findings, seconds=time.monotonic() - started, preflight=prepared.document())
    steps.ensure(project, host, ["buildfiles"])
    prepared.assert_current(project)
    python = python_visible(host)
    if python is not None:
        raise Held(
            cause_named(
                "tools.path",
                f"tools.path: {python} is visible; [tools].path must not contain Python",
                owner="build",
                stage="check",
            )
        )
    try:
        prepared.assert_current(project)
        process.run_native(
            make_command(host, "check"),
            project.root,
            "check",
            env=environment(host),
            temporary_root=project.build,
            context={"target": "check", "versions": list(project.versions)},
        )
    except Held as error:
        output = [
            line
            for result in process.native_results(error.fault)
            for line in (result.stdout + result.stderr).splitlines()
        ]
        return Outcome(
            False,
            True,
            findings,
            output[-20:],
            time.monotonic() - started,
            capture(error, cause=cause_named("build.unexpected", str(error), owner="build", stage="build")).document(),
        )
    return Outcome(not findings, True, findings, [], time.monotonic() - started)
