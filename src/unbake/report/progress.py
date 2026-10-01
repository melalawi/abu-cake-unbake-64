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
from unbake.report import files
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


def progress_line(version: str, document: dict[str, Any]) -> str:
    """Use report measures; floor matched cells and ceil additional fuzzy cells."""
    measures = _measures(document, version)
    counts = []
    for field in ("matched_code", "total_code"):
        value = measures.get(field, 0)  # Native proto3 reports omit zero-valued fields.
        if isinstance(value, str) and re.fullmatch("[0-9]+", value):
            value = int(value)
        if type(value) is not int or value < 0:
            raise Held("report", f"{version}.measures.{field}: invalid native counter")
        counts.append(value)
    matched, total = counts
    if matched > total:
        raise Held("report", f"{version}.measures: matched_code exceeds total_code")
    percentages = []
    for field in ("matched_code_percent", "fuzzy_match_percent"):
        value = measures.get(field, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            raise Held("report", f"{version}.measures.{field}: invalid percentage")
        percentages.append(float(value))
    matched_percent, fuzzy_percent = percentages
    matched_cells = math.floor(matched_percent / 5)
    fuzzy_cells = min(20 - matched_cells, math.ceil(max(0, fuzzy_percent - matched_percent) / 5))
    bar = "█" * matched_cells + "▒" * fuzzy_cells + "░" * (20 - matched_cells - fuzzy_cells)
    return f"{version} [{bar}]  {matched_percent:.2f}% (~{fuzzy_percent:.2f}%)  {matched:,} of {total:,} bytes"


def progress(reports: dict[str, dict[str, Any]], descriptions: dict[str, str]) -> str:
    """Render one progress line per VERSION, in report/configuration order."""
    if not reports:
        raise Held("report", "reports: missing VERSION values")
    blocks = []
    for version, document in reports.items():
        description = descriptions.get(version)
        if not isinstance(description, str) or not description.strip():
            raise Held("report", f"readme.descriptions.{version}: missing value")
        if "\n" in description or "|" in description:
            raise Held("report", f"readme.descriptions.{version}: invalid table description")
        line = progress_line(version, document)
        blocks.append(f"| {description} |\n|---|\n| <pre><code>{line}</code></pre> |")
    return "\n\n".join(blocks)


def render(template: str, reports: dict[str, dict[str, Any]]) -> str:
    """Replace existing progress figures while preserving surrounding README text."""
    heading = "## Progress\n\n"
    if template.count(heading) != 1:
        raise Held("report", "readme.Progress: exactly one heading required")
    before, body = template.split(heading)
    end = body.find("\n## ")
    if end < 0:
        raise Held("report", "readme.Progress: following section missing")
    block = body[:end]
    descriptions = {}
    matches = []
    for version in reports:
        match = re.search(r"^\| (" + re.escape(version) + r" \([^\n|]+) \|$", block, re.MULTILINE)
        if match is None:
            raise Held("report", f"readme.descriptions.{version}: missing value")
        descriptions[version] = match[1]
        matches.append(match)
    if not reports:
        raise Held("report", "reports: missing VERSION values")
    # Initial project templates contain only description rows, before any figures exist.
    if "<pre>" not in block:
        return before + heading + progress(reports, descriptions) + "\n" + body[end:]
    spans = []
    tables = []
    for version, match in zip(reports, matches, strict=True):
        following = re.search(r"^\| [\w-]+ \([^\n|]+ \|$", block[match.end() :], re.MULTILINE)
        limit = match.end() + following.start() if following else len(block)
        figures = re.search(r"<pre>(.*?)</pre>", block[match.end() : limit], re.DOTALL)
        if figures is None:
            raise Held("report", f"readme.Progress.{version}: progress block missing")
        start = match.end() + figures.start(1)
        stop = match.end() + figures.end(1)
        table_end = block.find("\n", match.end() + figures.end())
        if table_end < 0:
            table_end = len(block)
        line = "<code>" + progress_line(version, reports[version]) + "</code>"
        tables.append(block[match.start() : start] + line + block[stop:table_end])
        spans.append((match.start(), table_end))
    replacements = list(zip(sorted(spans), tables, strict=True))
    for (start, stop), table in reversed(replacements):
        block = block[:start] + table + block[stop:]
    # Remove the duplicate legacy summary and its separating blank line.
    block = re.sub(r"^<pre>.*?</pre>\n*", "", block, count=1, flags=re.DOTALL)
    return before + heading + block + body[end:]


def write(project: Project, policy: Policy) -> list[Path]:
    from unbake.project.build import current_generation

    tool = objdiff_cli(policy, "report")
    if not getattr(project, "versions", None):
        raise Held("report", "project.versions is missing")
    readme = project.root / "README.md"
    try:
        original = readme.read_text(encoding="utf-8")
    except OSError as error:
        raise Held("report", f"README {readme}: {error}") from error
    reports: dict[str, dict[str, Any]] = {}
    written: list[Path] = []
    pending: list[tuple[Path, bytes]] = []
    try:
        for version in project.versions:
            generation = current_generation(project, version)
            workspace = generation / "report"
            workspace.mkdir(parents=True, exist_ok=True)
            units = report_units.units(project, policy, version, generation, workspace)
            config = generation / "objdiff.json"
            files.write(
                config,
                (json.dumps({"build_base": False, "build_target": False, "units": units}, indent=2) + "\n").encode(),
            )
            destination = project.root / "versions" / version / "report.json"
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
                reports[version] = document
                pending.append((destination, (json.dumps(document, indent=2) + "\n").encode()))
            written.extend((project.build_link(version) / "objdiff.json", destination))
        rendered = render(original, reports)
        for destination, content in pending:
            files.write(destination, content)
        files.write(readme, rendered.encode())
        written.append(readme)
    except OSError as error:
        raise Held("report", f"report file/tool: {error}") from error
    return written
