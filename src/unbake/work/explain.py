"""explain: where one function stands and why, in named sections."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake import extract
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import named as cause_named
from unbake.work import attempts, compare, plan


@dataclass
class Report:
    function: str
    sections: dict[str, Any] = field(default_factory=dict)
    next_words: tuple[str, ...] | None = None

    def document(self) -> dict[str, Any]:
        return {"function": self.function, "sections": self.sections}

    def lines(self) -> list[str]:
        output = []
        for name, value in self.sections.items():
            output.append(f"[{name}]")
            if isinstance(value, str):
                output.extend(value.rstrip().splitlines())
            elif isinstance(value, list):
                output.extend(str(item) for item in value)
            else:
                output.extend(f"{key}: {item}" for key, item in value.items())
        return output


def subject_of(project: Project, subject: str) -> tuple[str, Path | None]:
    """A FUNC name, or a FILE whose stem names the function."""
    if re.fullmatch(r"[A-Za-z_]\w*", subject):
        file = project.work / subject / f"{subject}.c"
        return subject, file if file.is_file() else None
    path = Path(subject).resolve()
    return compare.function_of(path), path


def _status(project: Project, function: str, file: Path | None) -> dict[str, Any]:
    versions = split.holding_versions(project, function)
    rows = {version: compare.row_of(project, function, version) for version in versions}
    summary = attempts.ledger(project).summaries().get(function)
    return {
        "versions": ", ".join(versions),
        "bytes": {version: row.end - row.start for version, row in rows.items()},
        "kind": {version: row.kind for version, row in rows.items()},
        "draft": str(file) if file else "none",
        "attempts": summary.attempts if summary else 0,
        "best_percent": summary.best_percent if summary else None,
        "exact": summary.exact if summary else False,
    }


def _types(project: Project, function: str) -> str:
    from unbake.typemap import database

    return database.context(project, function=function)


def _rodata(project: Project, function: str) -> list[dict[str, object]]:
    from unbake.layout import rodata_owners

    census = rodata_owners.scan(project, project.names_from)
    return [item.document() for item in census.objects if function in item.owners]


def _needs(project: Project, host: Host, function: str) -> str:
    from unbake.decomp import guide

    return guide.run(project, host, function, None)


def _order(project: Project, host: Host, function: str, file: Path | None) -> Any:
    from unbake.decomp import explain as allocation_explain

    if file is None:
        raise Held(
            cause_named(
                "explain.order",
                f"explain.order: {function}: needs a draft file",
                owner="work.explain",
                stage="explain",
            )
        )
    versions = split.holding_versions(project, function)
    version = project.names_from if project.names_from in versions else versions[0]
    schedule = allocation_explain.order(project, host, file, version)
    return dataclasses.asdict(schedule) if dataclasses.is_dataclass(schedule) else str(schedule)


def _similar(project: Project, host: Host, function: str) -> list[dict[str, Any]]:
    from unbake.decomp import similar

    versions = split.holding_versions(project, function)
    version = project.names_from if project.names_from in versions else versions[0]
    examples = similar.retrieve(project, function, version, extract.directory(project, host, version))
    return [
        {
            "function": item.function,
            "distance": round(item.distance, 6),
            "edits": item.edit_distance,
            "source": str(item.source),
        }
        for item in examples
    ]


def explain(project: Project, host: Host, subject: str, sections: tuple[str, ...]) -> Report:
    function, file = subject_of(project, subject)
    report = Report(function)
    for name in sections:
        if name == "status":
            report.sections[name] = _status(project, function, file)
        elif name == "types":
            report.sections[name] = _types(project, function)
        elif name == "rodata":
            report.sections[name] = _rodata(project, function)
        elif name == "needs":
            report.sections[name] = _needs(project, host, function)
        elif name == "order":
            report.sections[name] = _order(project, host, function, file)
        elif name == "similar":
            report.sections[name] = _similar(project, host, function)
        else:
            raise Held(
                cause_named(
                    "explain.section",
                    f"explain.section: {name}: unknown section",
                    owner="work.explain",
                    stage="explain",
                )
            )
    eligible = False
    if "status" in report.sections:
        eligible = any(
            row.function == function for row in plan.candidates(project, host, selected=frozenset({function}))
        )
        report.sections["status"]["cycle"] = {
            "eligible": eligible,
            "reason": "candidate (size window not applied)" if eligible else plan.refusal(project, function),
        }
    if file is not None:
        report.next_words = ("compare", str(file))
    elif eligible:
        report.next_words = ("draft", function)
    elif "status" not in report.sections:
        report.next_words = ("explain", function, "--section", "status")
    return report
