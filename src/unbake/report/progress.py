"""Progress reports (objdiff report schema v2) and the README progress block derived from them."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, cast

from unbake.config import Held, Host, Project
from unbake.report import files, readme_layout
from unbake.report import units as report_units


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


def _retain_spacing(original: str, generated: str) -> str:
    """Keep owner spacing even when a percentage gains or loses a digit."""
    table_pattern = r"^\| ([\w-]+) \([^\n|]+ \|\r?$"

    def key(match: re.Match[str], tables: list[tuple[int, str]]) -> tuple[str, str]:
        label = match["label"]
        version = ""
        if label in {"bytes", "functions"}:
            version = next((name for offset, name in reversed(tables) if offset < match.start()), "")
        return version, label

    tables = [(match.start(), match[1]) for match in re.finditer(table_pattern, original, re.MULTILINE)]
    fields = ("pad", "percent_pad", "count_pad")
    spacing = {key(match, tables): tuple(match[field] for field in fields) for match in _FIGURE.finditer(original)}
    tables = [(match.start(), match[1]) for match in re.finditer(table_pattern, generated, re.MULTILINE)]

    def replace(match: re.Match[str]) -> str:
        pads = spacing.get(key(match, tables))
        content = match[0]
        if pads is not None:
            for field, pad in reversed(list(zip(fields, pads, strict=True))):
                start, stop = match.span(field)
                content = content[: start - match.start()] + pad + content[stop - match.start() :]
        return content

    return _FIGURE.sub(replace, generated)


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


def render(template: str, reports: dict[str, dict[str, Any]], *, descriptions: dict[str, str] | None = None) -> str:
    """Update the Progress body and retain every other owner byte."""
    before, block, after = readme_layout.section(template)
    if not reports:
        raise Held("report", "reports: missing VERSION values")
    if descriptions is not None:
        newline = "\r\n" if before.endswith("\r\n") else "\n"
        generated_body = _retain_spacing(block, progress(reports, descriptions))
        return before + generated_body.replace("\n", newline) + newline + after
    # A unique configured rename selects the matching report, while the live
    # table description and summary label remain the owner's text.
    labels = re.findall(r"^\| ([\w-]+) \([^\n|]+ \|\r?$", block, re.MULTILINE)
    missing = set(reports) - set(labels)
    obsolete = set(labels) - set(reports)
    if len(missing) == len(obsolete) == 1:
        old, new = next(iter(obsolete)), next(iter(missing))
        reports = {old if version == new else version: document for version, document in reports.items()}
    descriptions = {}
    matches = []
    for version in reports:
        match = re.search(r"^\| (" + re.escape(version) + r" \([^\n|]+) \|(?=\r?$)", block, re.MULTILINE)
        if match is None:
            raise Held("report", f"readme.descriptions.{version}: missing value")
        descriptions[version] = match[1]
        matches.append((version, match))
    if "<pre>" not in block:
        # Init supplies empty tables. Insert their generated rows into the live
        # layout; descriptions, whitespace and prose belong to the owner.
        newline = "\r\n" if before.endswith("\r\n") else "\n"
        ordered = sorted(matches, key=lambda item: item[1].start())
        generated = progress({version: reports[version] for version, _ in ordered}, descriptions).split("\n\n")
        rows = generated[-len(reports) :]
        for (_, match), row in reversed(list(zip(ordered, rows, strict=True))):
            delimiter = re.match(r"\r?\n\|---\|", block[match.end() :])
            position = match.end() + delimiter.end() if delimiter else match.end()
            generated_row = row.split("\n")[-1]
            if delimiter is None:
                generated_row = "|---|" + newline + generated_row
            block = block[:position] + newline + generated_row + block[position:]
        if len(reports) > 1:
            block = generated[0] + newline * 2 + block
        return before + block + after
    replacements = []
    for version, match in matches:
        following = re.search(r"^\| [\w-]+ \([^\n|]+ \|\r?$", block[match.end() :], re.MULTILINE)
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


def _unit(row: report_units.Function, best: float | None) -> dict[str, Any]:
    size = row.end - row.start
    matched = row.kind == "c"
    measures: dict[str, Any] = {
        "total_code": str(size),
        "matched_data_percent": 100.0,
        "total_functions": 1,
        "complete_data_percent": 100.0,
        "total_units": 1,
    }
    function: dict[str, Any] = {"name": row.name, "size": str(size), "metadata": {}, "address": "0"}
    section: dict[str, Any] = {"name": ".text", "size": str(size), "metadata": {}}
    fuzzy = 100.0 if matched else best
    if fuzzy is not None:
        measures["fuzzy_match_percent"] = fuzzy
        function["fuzzy_match_percent"] = fuzzy
        section["fuzzy_match_percent"] = fuzzy
    if matched:
        measures.update(
            matched_code=str(size),
            matched_code_percent=100.0,
            matched_functions=1,
            matched_functions_percent=100.0,
            complete_code=str(size),
            complete_code_percent=100.0,
            complete_units=1,
        )
    metadata: dict[str, Any] = {"complete": matched}
    if matched:
        metadata["source_path"] = f"src/{row.path}.c"
    return {
        "name": row.name,
        "measures": measures,
        "sections": [section],
        "functions": [function],
        "metadata": metadata,
    }


def measure(project: Project, policy: Host, version: str) -> dict[str, Any]:
    """Progress of one version from its split rows and the best attempt of each unmatched function."""
    from unbake.work import attempts

    rows = report_units.functions(project.version(version))
    best = {}
    for function in attempts.functions(project):
        found = attempts.read(project, function)
        scores = [row.versions[version]["percent"] for row in found if version in row.versions]
        if scores:
            best[function] = max(scores)
    units = [_unit(row, best.get(row.name)) for row in rows]
    total = sum(row.end - row.start for row in rows)
    matched_rows = [row for row in rows if row.kind == "c"]
    matched = sum(row.end - row.start for row in matched_rows)
    fuzzy_bytes = sum(
        (row.end - row.start) * (100.0 if row.kind == "c" else best.get(row.name, 0.0)) / 100.0 for row in rows
    )

    def share(part: float, whole: float) -> float:
        return 100.0 * part / whole if whole else 0.0

    measures = {
        "fuzzy_match_percent": share(fuzzy_bytes, total),
        "total_code": str(total),
        "matched_code": str(matched),
        "matched_code_percent": share(matched, total),
        "matched_data_percent": 100.0,
        "total_functions": len(rows),
        "matched_functions": len(matched_rows),
        "matched_functions_percent": share(len(matched_rows), len(rows)),
        "complete_code": str(matched),
        "complete_code_percent": share(matched, total),
        "complete_data_percent": 100.0,
        "total_units": len(rows),
        "complete_units": len(matched_rows),
    }
    return {"measures": measures, "units": units, "version": 2}


def findings(project: Project, policy: Host) -> list[str]:
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
            # Equal totals can conceal merged, renamed or reclassified rows.
            # Compare each VERSION's own inventory, including its open state.
            current_units = [(unit["name"], unit.get("metadata", {}).get("complete")) for unit in current["units"]]
            saved_units = [
                (unit.get("name"), unit.get("metadata", {}).get("complete")) for unit in saved.get("units", [])
            ]
            if current_units != saved_units or any(
                actual.get(field, 0) != expected.get(field, 0) for field in actual.keys() | expected.keys()
            ):
                lines.append(f"HELD(check): stale report VERSION {version}: run unbake check")
        except Held as error:
            lines.append(f"HELD(check): report VERSION {version}: {error.reason}")
    return lines


def readme_descriptions(project: Project) -> dict[str, str]:
    """Require owner release labels and use each version's supplied ROM identity."""
    descriptions = {}
    for name in project.versions:
        version = project.version(name)
        for field in ("cartridge_id", "region", "description"):
            value = getattr(version, field)
            if not isinstance(value, str) or not value.strip():
                raise Held("report", f"version.{name}.{field}: missing value")
            if any(char in value for char in "\r\n|"):
                raise Held("report", f"version.{name}.{field}: invalid table text")
        try:
            with version.baserom.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as error:
            raise Held("report", f"version.{name}.baserom: {error}") from error
        descriptions[name] = (
            f"{name} ({version.cartridge_id}, {version.region}). {version.description} SHA256 `{digest}`"
        )
    return {
        name: descriptions[name] for name in sorted(descriptions, key=lambda name: project.version(name).cartridge_id)
    }


def write(project: Project, policy: Host, *, reports: dict[str, dict[str, Any]] | None = None) -> list[Path]:
    if not project.versions:
        raise Held("report", "project.versions is missing")
    descriptions = readme_descriptions(project)
    if reports is None:
        reports = {v: measure(project, policy, v) for v in project.versions}
    readme = project.root / "README.md"
    if set(reports) != set(project.versions):
        raise Held("report", "reports: expected every configured VERSION exactly once")
    reports = {name: reports[name] for name in descriptions}
    for version, document in reports.items():
        if "units" in document:
            expected_units = [(row.name, row.kind == "c") for row in report_units.functions(project.version(version))]
            reported_units = [(unit["name"], unit.get("metadata", {}).get("complete")) for unit in document["units"]]
            if reported_units != expected_units:
                raise Held("report", f"VERSION {version}: function rows changed; regenerate report")
    try:
        if readme.exists():
            original = readme.read_bytes().decode("utf-8", errors="surrogateescape")
        else:
            original = (
                (Path(__file__).parents[1] / "templates" / "README.ready.md")
                .read_text()
                .replace("@TITLE@", project.title)
            )
        rendered = render(original, reports, descriptions=descriptions)
        written: list[Path] = []
        for version, document in reports.items():
            destination = project.root / "versions" / version / "report.json"
            files.write(destination, (json.dumps(document, indent=2) + "\n").encode())
            written.append(destination)
        files.write(readme, rendered.encode("utf-8", errors="surrogateescape"))
        written.append(readme)
    except OSError as error:
        raise Held("report", f"report file/tool: {error}") from error
    return written
