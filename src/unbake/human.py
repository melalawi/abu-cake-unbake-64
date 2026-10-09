"""Human findings, command feedback, and terminal progress."""
from __future__ import annotations

import shutil
from collections.abc import Sequence
from typing import TextIO

from unbake import config, effort
from unbake.contracts import Finding, Json, StageRecord

_stream: TextIO | None = None
_tty = False
_live = False
_last = ""


def attach(stream: TextIO, tty: bool) -> None:
    global _stream, _tty
    _stream, _tty = stream, tty
    effort.listen(_event)


def _clear() -> None:
    global _live, _last
    if _live and _stream is not None:
        _stream.write("\r\x1b[K")
        _stream.flush()
    _live, _last = False, ""


def _event(event: str, body: Json) -> None:
    global _live, _last
    if not _tty or _stream is None:
        return
    if event == "close" and len(body["path"]) == 1:
        _clear()
        return
    if event == "open":
        text = " > ".join(body["path"])
    elif event == "progress":
        text = f"{body['name']} {body['done']}/{body['total']}"
    else:
        return
    width = shutil.get_terminal_size().columns - 1
    _last = text[:width]
    _stream.write("\r\x1b[K" + _last)
    _stream.flush()
    _live = True


def finding(finding: Finding) -> str:
    f = finding
    lines = [f"{'Refused' if f.blocking else 'Debt'}: {config.sentence(f.key)}."]
    if f.reason:
        lines.append(f"  because {f.reason}")
    if f.path:
        lines.append(f"  in {f.path}" + (f":{f.line}" if f.line > 0 else ""))
    if f.unit:
        lines.append(f"  item {f.unit}")
    if f.versions:
        lines.append("  versions " + ", ".join(f.versions))
    lines.extend(f"  missing {missing}" for missing in f.missing)
    if f.action:
        lines.append(f"  next: {f.action}")
    return "\n".join(lines)


def summary(records: Sequence[StageRecord]) -> list[str]:
    root = next((r for r in reversed(records) if r.kind == "command"), None)
    if root is None:
        return []
    lines = [f"took {root.wall_seconds:.2f}s, {root.cores:.2f} cores "
             f"(parent {root.parent_cpu_seconds:.2f}s, workers {root.worker_cpu_seconds:.2f}s, "
             f"native {root.native_cpu_seconds:.2f}s cpu)"]
    memory = []
    for label, field in (("parent", "parent"), ("workers", "worker")):
        key = f"resources.memory_{field}_bytes"
        origin = root.memory_limits.get(key)
        memory.append(f"{label} peak {getattr(root, field + '_rss_peak_bytes') / 1e6:.1f} MB "
                      f"of {root.memory_limit_values.get(key, 0) / 1e6:.1f} MB "
                      f"({origin.file if origin else 'unbound'})")
    lines.append("memory: " + ", ".join(memory))
    lines.extend(f"cache: {kind} {hits} warm / {misses} cold"
                 for kind, (hits, misses) in sorted(root.cache.items()))
    lines.append("slowest:")
    lines.extend(f"  {' > '.join(r.path)}  {r.wall_seconds:.2f}s  {r.cores:.2f} cores  {r.execution}  items {r.items} "
                 f"jobs {r.jobs} workers {r.workers_used}/{r.workers_admitted}"
                 for r in sorted(records, key=lambda r: r.wall_seconds, reverse=True)[:10])
    lines.extend(f"slow: {config.sentence(f.key)} {f.reason}"
                 for record in records for f in record.findings if f.key.startswith("budget."))
    return lines


def finish(command: str, result: Json | None, findings: Sequence[Finding]) -> None:
    _clear()
    if _stream is None:
        return
    lines = [finding(f) for f in sorted(findings, key=lambda f: not f.blocking)]
    if result is not None:
        feedback = result.get("feedback")
        label = result.get("label", feedback.get("label") if feedback else None)
        member = result.get("member", feedback.get("member") if feedback else "")
        if label in ("CRACKED", "NEEDS CREATIVE"):
            colour = "32" if label == "CRACKED" else "33"
            rendered = f"\x1b[{colour}m{label}\x1b[0m" if _tty else label
            lines.append(f"{member}: {rendered}")
        if feedback is not None:
            lines.append(f"{member} ({feedback['subsystem']}): {feedback['score_before'] * 100:.1f}% -> "
                         f"{feedback['score_after'] * 100:.1f}% ({feedback['outcome']})")
            for hint in feedback["hints"]:
                lines.append(f"  hint {hint['id']}: {hint['technique']}")
                lines.extend(f"    because {key}={value}" for key, value in hint["because"].items())
            lines.append(f"  next: {feedback['next']}")
    lines.extend(summary(effort.closed()))
    for line in lines:
        _stream.write(line + "\n")
    _stream.flush()
