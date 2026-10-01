"""Native objdiff reports and README progress derived only from those reports."""

from __future__ import annotations

import json
import math
import re
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

from unbake.decomp.score import objdiff_cli
from unbake.project.config import Held, Policy, Project
from unbake.report import files, readme_layout
from unbake.report import units as report_units


def target_object(function: str, code: bytes) -> bytes:
    """Wrap a cartridge function in a big-endian MIPS ELF32 relocatable object."""
    if not isinstance(function, str) or not function or "\x00" in function:
        raise Held("report", "target_object.function is missing or invalid")
    if not code or len(code) % 4:
        raise Held("report", f"target_object.code for {function} must contain whole MIPS words")
    strings = b"\x00" + function.encode("utf-8") + b"\x00"
    section_names = b"\x00.text\x00.symtab\x00.strtab\x00.shstrtab\x00"
    symbols = bytes(16) + struct.pack(">IIIBBH", 0, 0, 0, 3, 0, 1) + struct.pack(">IIIBBH", 1, 0, len(code), 18, 0, 1)
    content = bytearray(bytes(52))
    headers = [bytes(40)]
    for name, kind, flags, data, link, info, alignment, entry_size in (
        (".text", 1, 6, code, 0, 0, 4, 0),
        (".symtab", 2, 0, symbols, 3, 2, 4, 16),
        (".strtab", 3, 0, strings, 0, 0, 1, 0),
        (".shstrtab", 3, 0, section_names, 0, 0, 1, 0),
    ):
        content.extend(bytes(-len(content) % alignment))
        offset = len(content)
        content.extend(data)
        headers.append(
            struct.pack(
                ">IIIIIIIIII",
                section_names.index(name.encode() + b"\x00"),
                kind,
                flags,
                0,
                offset,
                len(data),
                link,
                info,
                alignment,
                entry_size,
            )
        )
    content.extend(bytes(-len(content) % 4))
    section_offset = len(content)
    content.extend(b"".join(headers))
    content[:52] = (
        b"\x7fELF\x01\x02\x01"
        + bytes(9)
        + struct.pack(">HHIIIIIHHHHHH", 1, 8, 1, 0, 0, section_offset, 536875009, 52, 0, 0, 40, 5, 4)
    )
    return bytes(content)


def _json(path: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads(path.read_bytes()))
    except (OSError, ValueError) as error:
        raise Held("report", f"objdiff report {path}: {error}") from error


def _measures(document: Any, name: str | Path) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("version") != 2:
        raise Held("report", f"objdiff report {name}.version must be 2")
    measures = document.get("measures")
    if not isinstance(measures, dict):
        raise Held("report", f"objdiff report {name}.measures is missing")
    return measures


def _native_counts(document: Any, name: str | Path) -> None:
    """Materialize proto3 counters emitted as decimal strings or omitted zeroes."""
    measures = _measures(document, name)
    for field in ("matched_code", "complete_code", "total_code", "complete_units", "total_units"):
        value = measures.get(field, 0)
        if isinstance(value, str) and re.fullmatch("[0-9]+", value):
            value = int(value)
        if type(value) is not int or value < 0:
            raise Held("report", f"{name}.measures.{field}: invalid native counter")
        measures[field] = value


def _counter(measures: dict[str, Any], field: str, name: str) -> int:
    value = measures.get(field, 0)
    if isinstance(value, str) and re.fullmatch("[0-9]+", value):
        value = int(value)
    if type(value) is not int or value < 0:
        raise Held("report", f"{name}.measures.{field}: invalid native counter")
    return value


def _percentage(measures: dict[str, Any], field: str, name: str) -> float:
    value = measures.get(field, 0)
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        raise Held("report", f"{name}.measures.{field}: invalid percentage")
    return float(value)


def _figures(document: dict[str, Any], name: str, functions: bool = False) -> tuple[int, int, float, float]:
    measures = _measures(document, name)
    kind = "units" if functions else "code"
    complete = _counter(measures, "complete_" + kind, name)
    total = _counter(measures, "total_" + kind, name)
    if complete > total:
        raise Held("report", f"{name}.measures: complete_{kind} exceeds total_{kind}")
    percent = 100 * complete / total if total else 0.0
    fuzzy = percent if functions else _percentage(measures, "fuzzy_match_percent", name)
    return complete, total, percent, fuzzy


def _bar(percent: float, fuzzy: float) -> str:
    matched = math.floor(percent / 5)
    partial = min(20 - matched, math.ceil(max(0, fuzzy - percent) / 5))
    return "█" * matched + "▒" * partial + "░" * (20 - matched - partial)


def _line(label: str, document: dict[str, Any], version: str, functions: bool = False) -> str:
    matched, total, percent, fuzzy = _figures(document, version, functions)
    suffix = "" if functions else f" (~{fuzzy:.2f}%)"
    return f"{label} [{_bar(percent, fuzzy)}]  {percent:5.2f}%{suffix}  {matched:,} of {total:,}"


def progress(reports: dict[str, dict[str, Any]], descriptions: dict[str, str]) -> str:
    """Create the established bytes/functions table layout for a new README."""
    if not reports:
        raise Held("report", "reports: missing VERSION values")
    blocks = []
    for version, document in reports.items():
        description = descriptions.get(version)
        if not isinstance(description, str) or not description.strip():
            raise Held("report", f"readme.descriptions.{version}: missing value")
        if "\n" in description or "|" in description:
            raise Held("report", f"readme.descriptions.{version}: invalid table description")
        byte_line = _line("bytes    ", document, version)
        function_line = _line("functions", document, version, functions=True)
        blocks.append(
            f"| {description} |\n|---|\n| <pre><code>{byte_line}</code><br><code>{function_line}</code></pre> |"
        )
    if len(reports) > 1:
        summaries = {"all": _aggregate(reports), **reports}
        width = max(map(len, summaries))
        lines = [
            f"<code>{_line(version.ljust(width), document, version)} bytes</code>"
            for version, document in summaries.items()
        ]
        blocks.insert(0, "<pre>" + "<br>".join(lines) + "</pre>")
    return "\n\n".join(blocks)


_FIGURE = re.compile(
    r"(?P<label>[\w-]+)(?P<pad> +)\[[#\-█▒░]{20}\]"
    r"(?P<percent_pad> +)(?P<percent>[0-9]+\.[0-9]+)%"
    r"(?: \(~[0-9]+\.[0-9]+%\))?(?P<count_pad> +)"
    r"[0-9,]+ of [0-9,]+(?P<suffix> bytes)?"
)


def _replace_figures(content: str, document: dict[str, Any], version: str, table: bool) -> str:
    expected = {"bytes", "functions"} if table else {version}
    seen: list[str] = []

    def replace(match: re.Match[str]) -> str:
        label = match["label"]
        if label not in expected:
            raise Held("report", f"readme.Progress.{version}: unexpected label {label}")
        seen.append(label)
        functions = label == "functions"
        matched, total, percent, fuzzy = _figures(document, version, functions)
        width = len(match["percent_pad"]) + len(match["percent"])
        percentage = f"{percent:.2f}"
        percentage = percentage.rjust(max(width, len(percentage) + 1))
        fuzzy_text = "" if functions else f" (~{fuzzy:.2f}%)"
        return (
            f"{label}{match['pad']}[{_bar(percent, fuzzy)}]{percentage}%{fuzzy_text}"
            f"{match['count_pad']}{matched:,} of {total:,}{match['suffix'] or ''}"
        )

    updated = _FIGURE.sub(replace, content)
    if set(seen) != expected or len(seen) != len(expected):
        raise Held("report", f"readme.Progress.{version}: bytes/functions progress block missing or duplicated")
    return updated


def _aggregate(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    figures = [_figures(document, version) for version, document in reports.items()]
    matched = sum(row[0] for row in figures)
    total = sum(row[1] for row in figures)
    fuzzy = sum(row[1] * row[3] for row in figures) / total if total else 0.0
    return {
        "version": 2,
        "measures": {
            "complete_code": matched,
            "total_code": total,
            "fuzzy_match_percent": fuzzy,
        },
    }


def render(template: str, reports: dict[str, dict[str, Any]]) -> str:
    """Update only figures in existing summaries and VERSION or bytes/functions tables."""
    before, block, after = readme_layout.section(template)
    if not reports:
        raise Held("report", "reports: missing VERSION values")
    # Reconcile a uniquely renamed VERSION while retaining the established layout.
    labels = re.findall(r"^\| ([\w-]+) \([^\n|]+ \|$", block, re.MULTILINE)
    missing = set(reports) - set(labels)
    obsolete = set(labels) - set(reports)
    if len(missing) == len(obsolete) == 1:
        old, new = next(iter(obsolete)), next(iter(missing))
        block = re.sub(r"(?m)^(\| )" + re.escape(old) + r"(?= \()", lambda m: m[1] + new, block)

        def rename_summary(match: re.Match[str]) -> str:
            width = len(old) + len(match[1])
            return new + " " * max(1, width - len(new)) + "["

        block = re.sub(r"(?<=<code>)" + re.escape(old) + r"( +)\[", rename_summary, block)
    descriptions = {}
    matches = []
    for version in reports:
        match = re.search(r"^\| (" + re.escape(version) + r" \([^\n|]+) \|$", block, re.MULTILINE)
        if match is None:
            raise Held("report", f"readme.descriptions.{version}: missing value")
        descriptions[version] = match[1]
        matches.append((version, match))
    if "<pre>" not in block:
        return before + progress(reports, descriptions) + "\n" + after
    replacements = []
    for version, match in matches:
        following = re.search(r"^\| [\w-]+ \([^\n|]+ \|$", block[match.end() :], re.MULTILINE)
        limit = match.end() + following.start() if following else len(block)
        figures = re.search(r"<pre>(.*?)</pre>", block[match.end() : limit], re.DOTALL)
        if figures is None:
            raise Held("report", f"readme.Progress.{version}: progress block missing")
        start = match.end() + figures.start(1)
        stop = match.end() + figures.end(1)
        replacement = _replace_figures(figures[1], reports[version], version, table=True)
        replacements.append((start, stop, replacement))
    if not readme_layout.complete(block):
        raise Held("report", "readme.Progress: complete bytes/functions tables and summary required")
    # Summary labels and their order belong to the template, including its all line.
    first_table = min(match.start() for _, match in matches)
    summary = block[:first_table]
    summary_reports = {**reports, "all": _aggregate(reports)}
    for code in re.finditer(r"<code>(.*?)</code>", summary, re.DOTALL):
        figure = _FIGURE.fullmatch(code[1])
        if figure is None or figure["label"] not in summary_reports:
            raise Held("report", "readme.Progress: invalid summary label or figures")
        version = figure["label"]
        replacements.append(
            (code.start(1), code.end(1), _replace_figures(code[1], summary_reports[version], version, table=False))
        )
    for start, stop, replacement in sorted(replacements, reverse=True):
        block = block[:start] + replacement + block[stop:]
    return before + block + after


def measure(project: Project, policy: Policy, version: str) -> dict[str, Any]:
    """Generate native totals from the current split and build without publishing them."""
    from unbake.project.build import current_generation

    tool = objdiff_cli(policy, "report")
    try:
        generation = current_generation(project, version)
        workspace = generation / "report"
        workspace.mkdir(parents=True, exist_ok=True)
        units = report_units.units(project, policy, version, generation, workspace)
        config = generation / "objdiff.json"
        files.write(
            config,
            (json.dumps({"build_base": False, "build_target": False, "units": units}, indent=2) + "\n").encode(),
        )
        with tempfile.TemporaryDirectory(dir=workspace, prefix="generate-") as temporary:
            output = Path(temporary) / "report.json"
            result = subprocess.run(
                [
                    str(tool),
                    "report",
                    "generate",
                    "--project",
                    str(generation),
                    "--output",
                    str(output),
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise Held("report", f"objdiff report generate VERSION {version}: {result.stderr.strip()}")
            document = _json(output)
            _native_counts(document, output)
            reported_units = document.get("units")
            if not isinstance(reported_units, list) or [unit.get("name") for unit in reported_units] != [
                unit["name"] for unit in units
            ]:
                raise Held("report", f"objdiff report {output}.units differ from objdiff.json")
            return document
    except OSError as error:
        raise Held("report", f"VERSION {version} report file/tool: {error}") from error


def findings(project: Project, policy: Policy) -> list[str]:
    """Name every VERSION whose saved native totals disagree with current inputs."""
    lines = []
    for version in project.versions:
        destination = project.root / "versions" / version / "report.json"
        try:
            current = measure(project, policy, version)
            saved = _json(destination)
            _native_counts(saved, destination)
            expected = _measures(current, version)
            actual = _measures(saved, destination)
            if any(actual.get(field, 0) != expected.get(field, 0) for field in actual.keys() | expected.keys()):
                lines.append(f"HELD(check): stale report VERSION {version}: run unbake report")
        except Held as error:
            lines.append(f"HELD(check): report VERSION {version}: {error.reason}")
    return lines


def write(project: Project, policy: Policy) -> list[Path]:
    if not project.versions:
        raise Held("report", "project.versions is missing")
    readme = project.root / "README.md"
    try:
        original = readme.read_text(encoding="utf-8")
        reports = {version: measure(project, policy, version) for version in project.versions}
        rendered = render(readme_layout.restore(original, project.root), reports)
        written: list[Path] = []
        for version, document in reports.items():
            destination = project.root / "versions" / version / "report.json"
            files.write(destination, (json.dumps(document, indent=2) + "\n").encode())
            written.extend((project.build_link(version) / "objdiff.json", destination))
        files.write(readme, rendered.encode())
        written.append(readme)
    except OSError as error:
        raise Held("report", f"report file/tool: {error}") from error
    return written
