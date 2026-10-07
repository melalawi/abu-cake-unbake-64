"""Progress reports (objdiff report schema v2) and the README progress block derived from them."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from pathlib import Path
from typing import Any, cast

from unbake import strict_json
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.report import files, readme_layout

# Bump when this step's output changes for the same inputs. Keys never digest the tool's code.
SCHEMA = 4
# Only C earns exact matched credit. Original assembly remains a separate denominator category.
DONE = {"c": ("c", "Matched C", ".c")}


def _json(path: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], strict_json.read(path))
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
    kind = "functions" if functions else "code"
    complete = _counter(measures, "matched_functions" if functions else "complete_code", name)
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
        draft: dict[str, Any] = next(
            (category["measures"] for category in document.get("categories", []) if category["id"] == "draft"), {}
        )
        detail = (
            f"Retained drafts: {int(draft.get('total_code', 0)):,} bytes / "
            f"{int(draft.get('total_functions', 0)):,} functions; "
            f"declared data: {int(document['measures'].get('total_data', 0)):,} bytes (matching unknown). "
            "Fuzzy % is known similarity; unknown scores remain unknown."
        )
        blocks.append(
            f"| {description} |\n|---|\n| <pre><code>{byte_line}</code><br>"
            f"<code>{function_line}</code></pre> {detail} |"
        )
    if len(reports) > 1:
        summaries = {"all": _aggregate(reports), **reports}
        width = max(map(len, summaries))
        lines = [
            f"<code>{_line(version.ljust(width), document, version)} bytes</code>"
            for version, document in summaries.items()
        ]
        data = int(summaries["all"]["measures"].get("total_data", 0))
        blocks.insert(
            0,
            "<pre>"
            + "<br>".join(lines)
            + "</pre>\n"
            + f"All versions: {data:,} declared data bytes (matching unknown); opaque binary assets excluded.",
        )
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
    units = [{"measures": report["measures"]} for report in reports.values()]
    measures = _sum_measures(units)
    measures.update(complete_code=matched, matched_code=matched, total_code=total, fuzzy_match_percent=fuzzy)
    return {"version": 2, "measures": measures}


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


def f32(value: float) -> float:
    """The shortest decimal that reads back as the same float32: objdiff report percentages are proto3 floats."""
    single = struct.unpack(">f", struct.pack(">f", value))[0]
    for digits in range(1, 10):
        shortest = float(f"{single:.{digits}g}")
        if struct.unpack(">f", struct.pack(">f", shortest))[0] == single:
            return shortest
    return float(single)


def _share(part: float, whole: float) -> float:
    return f32(100.0 * part / whole) if whole else 0.0


def _unit(
    row: split.Function,
    *,
    members: list[split.Function] | None = None,
    receipts: dict[str, dict[str, Any]] | None = None,
    version: str = "",
) -> dict[str, Any]:
    entries = members if members is not None else [row]
    matched = row.kind == "c"
    size = row.end - row.start
    functions = []
    weighted = 0.0
    draft = False
    for entry in entries:
        receipt_name = next(
            (name for name in entry.aliases if receipts is not None and name in receipts),
            None,
        )
        receipt = receipts[receipt_name] if receipts is not None and receipt_name is not None else None
        similarity = 100.0 if matched else (receipt["versions"][version] if receipt is not None else None)
        length = entry.end - entry.start
        function: dict[str, Any] = {
            "name": entry.name,
            "size": str(length),
            "metadata": {},
            "address": str(entry.address or 0),
        }
        if similarity is not None:
            function["fuzzy_match_percent"] = f32(similarity)
            weighted += length * similarity / 100
        draft |= receipt is not None
        functions.append(function)
    measures: dict[str, Any] = {
        "total_code": str(size),
        "total_functions": len(entries),
        "total_units": 1,
        "fuzzy_match_percent": _share(weighted, size),
    }
    if matched:
        measures.update(
            matched_code=str(size),
            complete_code=str(size),
            matched_code_percent=100.0,
            complete_code_percent=100.0,
            matched_functions=len(entries),
            matched_functions_percent=100.0,
            complete_units=1,
        )
    category = "c" if matched else "original_asm" if row.kind == "hasm" else "draft" if draft else "asm"
    metadata: dict[str, Any] = {"complete": matched, "progress_categories": [category]}
    if matched or row.kind == "hasm":
        metadata["source_path"] = f"src/{row.path}{'.c' if matched else '.s'}"
    elif draft:
        names = [name for entry in entries for name in entry.aliases if receipts is not None and name in receipts]
        metadata["source_path"] = f"src/{names[0]}.c"
    return {
        "name": Path(row.path).name,
        "measures": measures,
        "sections": [
            {"name": ".text", "size": str(size), "metadata": {}, "fuzzy_match_percent": _share(weighted, size)}
        ],
        "functions": functions,
        "metadata": metadata,
    }


def _sum_measures(units: list[dict[str, Any]]) -> dict[str, Any]:
    counters = (
        "total_code",
        "matched_code",
        "complete_code",
        "total_data",
        "matched_data",
        "complete_data",
        "total_functions",
        "matched_functions",
        "total_units",
        "complete_units",
    )
    result: dict[str, Any] = {field: sum(int(unit["measures"].get(field, 0)) for unit in units) for field in counters}
    for field, total in (
        ("matched_code", "total_code"),
        ("complete_code", "total_code"),
        ("matched_functions", "total_functions"),
    ):
        result[field + "_percent"] = _share(result[field], result[total])
    weighted = sum(
        int(unit["measures"].get("total_code", 0)) * unit["measures"].get("fuzzy_match_percent", 0) / 100
        for unit in units
    )
    result["fuzzy_match_percent"] = _share(weighted, result["total_code"])
    # Data matching is unknown: zero verified numerator, no fabricated percentage.
    return result


def measure(project: Project, policy: Host | None, version: str, *, current: Any = None) -> dict[str, Any]:
    """One current-source inventory; exact C, retained drafts, asm and declared data."""
    from unbake.layout import split
    from unbake.report import state

    if current is None:
        current = state.inventory(project)
    else:
        state.assert_current(project, current)
    units = [
        _unit(row, members=split.unit_members(row), receipts=current.receipts, version=version)
        for row in current.units[version]
    ]
    _, _, segments = split.layout(project.version(version).split)
    for segment in segments:
        for index, row in enumerate(segment.rows):
            if row.kind in split.CODE_KINDS:
                continue
            stop = segment.rows[index + 1].start if index + 1 < len(segment.rows) else segment.end
            if stop is None:
                raise Held("report", f"data.boundary: VERSION {version} {row.path}: missing end")
            units.append(
                {
                    "name": f"{row.kind}:{row.path}@{row.start:X}",
                    "measures": {"total_data": str(stop - row.start), "total_units": 1},
                    "sections": [{"name": row.kind, "size": str(stop - row.start), "metadata": {}}],
                    "functions": [],
                    "metadata": {"complete": False, "progress_categories": ["data"]},
                }
            )
    category_units: dict[str, list[dict[str, Any]]] = {
        kind: [] for kind in ("c", "original_asm", "draft", "asm", "data")
    }
    for row in current.units[version]:
        for member in split.unit_members(row):
            entry = _unit(member, members=[member], receipts=current.receipts, version=version)
            category_units[entry["metadata"]["progress_categories"][0]].append(entry)
    category_units["data"] = [unit for unit in units if "data" in unit["metadata"]["progress_categories"]]
    categories = [
        {"id": ident, "name": label, "measures": _sum_measures(category_units[ident])}
        for ident, label in (
            ("c", "Matched C"),
            ("original_asm", "Original asm"),
            ("draft", "Retained C draft"),
            ("asm", "Assembly"),
            ("data", "Declared data"),
        )
    ]
    return {"measures": _sum_measures(units), "units": units, "categories": categories, "version": 2}


def findings(project: Project, policy: Host) -> list[str]:
    """Name every VERSION whose saved native totals disagree with current inputs."""
    from unbake.report import state

    try:
        inventory = state.inventory(project)
    except Held as error:
        return [f"HELD(check): report: {error.reason}"]
    lines = []
    for version in project.versions:
        destination = project.root / "versions" / version / "report.json"
        try:
            current = measure(project, policy, version, current=inventory)
            saved = _json(destination)
            _native_counts(saved, destination)
            # Equal totals can conceal merged, renamed or reclassified rows.
            # Compare each VERSION's own inventory, including its open state.
            if saved != current:
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


def input_key(project: Project) -> str:
    """The owning report's source/receipt/layout/exporter dependency identity."""
    from unbake.cache import key
    from unbake.report import verify

    pins = verify.source_pins(project)
    tool = pins.get(verify.BUNDLE, "missing verifier payload")
    return key("progress", str(SCHEMA), json.dumps(pins, sort_keys=True), tool)


def owner_descriptions(project: Project, template: str) -> dict[str, str]:
    """Preserve the existing owning release and ROM identity labels for source-only regeneration."""
    descriptions = {}
    for version in project.versions:
        match = re.search(r"^\| (" + re.escape(version) + r" \([^\n|]+) \|$", template, re.M)
        if match is None:
            raise Held("report", f"readme.descriptions.{version}: owner label required for source-only regeneration")
        descriptions[version] = match[1]
    return descriptions


def write(
    project: Project,
    policy: Host | None,
    *,
    reports: dict[str, dict[str, Any]] | None = None,
    source_only: bool = False,
) -> list[Path]:
    """versions/*/report.json, the README progress block and attempts.json, the history they are measured from."""
    from unbake.layout import split
    from unbake.work import attempts

    if not project.versions:
        raise Held("report", "project.versions is missing")
    if source_only:
        # ROM identity text belongs to the existing owner README; this operation makes no ROM certification.
        template = (project.root / "README.md").read_text()
        descriptions = owner_descriptions(project, template)
    else:
        descriptions = readme_descriptions(project)
    from unbake.report import state, verify

    current = state.inventory(project)
    candidate_history = attempts.summaries(project)
    expected = {v: measure(project, policy, v, current=current) for v in project.versions}
    if reports is None:
        reports = expected
    readme = project.root / "README.md"
    if set(reports) != set(project.versions):
        raise Held("report", "reports: expected every configured VERSION exactly once")
    reports = {name: reports[name] for name in descriptions}
    for version, document in reports.items():
        if document != expected[version]:
            raise Held("report", f"VERSION {version}: report inventory or measures changed; regenerate report")
    # Strictly read every existing metadata file before the first write.
    rows = {
        alias
        for version in project.versions
        for row in current.units[version]
        for member in split.unit_members(row)
        for alias in member.aliases
    }
    omitted = [name for name, summary in candidate_history.items() if summary.fuzzy is not None and name not in rows]
    if omitted:
        raise Held("report", f"source.transition: {omitted[0]}: report rewrite would erase a retained receipt")
    prior_manifest = project.root / verify.MANIFEST
    if prior_manifest.is_file():
        _json(prior_manifest)
    for version in project.versions:
        previous = project.root / "versions" / version / "report.json"
        if previous.is_file():
            _json(previous)
    state.assert_current(project, current)
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
        written.append(attempts.write_summary(project, rows))
        manifest = project.root / verify.MANIFEST
        files.write(manifest, (json.dumps(verify.document(project, current, reports), indent=2) + "\n").encode())
        written.append(manifest)
    except OSError as error:
        raise Held("report", f"report file/tool: {error}") from error
    return written
