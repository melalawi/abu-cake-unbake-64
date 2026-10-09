"""Command line entry: builds the click group from commands.toml and runs one command under effort."""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Sequence
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Any

import click

from unbake import config, effort, human
from unbake.contracts import Finding, Json, Refusal


def _handler(name: str) -> Any:
    spec = config.load_resource("commands.toml")["command"][name]
    module, function = spec["handler"].split(".")
    return getattr(import_module(f"unbake.{module}"), function)

def _refused(result: Json | None) -> list[Finding]:
    """The findings of every entry a landing drain refused: a command whose work was refused did not succeed."""
    result = result or {}
    drain = (result["drain"] or {}) if "drain" in result else result  # a command that drained nothing holds None
    return [Finding(**{**f, "versions": tuple(f["versions"]), "missing": tuple(f["missing"])})
            for body in drain.get("refused", ()) for f in body["findings"]]

def _emit(name: str, code: int, result: Json | None, findings: Sequence[Finding]) -> int:
    if code != 0 and not findings:  # a failure always names its cause
        findings = [Finding("internal.no-cause", f"{name} exited {code} without any finding naming why")]
    ordered = sorted(findings, key=lambda f: not f.blocking)
    document = {
        "ok": code == 0,
        "invocation": effort.invocation(),
        "result": result,
        "findings": [asdict(f) for f in ordered],
        "stages": effort.tree(),
    }
    sys.stdout.write(json.dumps(document, default=str) + "\n")
    human.finish(name, result, ordered)
    return code

def _run(name: str, params: dict[str, Any], argv: Sequence[str]) -> int:
    result: Json | None = None
    findings: list[Finding] = []
    code = 0
    try:
        with effort.command(name, argv):
            host = params.pop("host")
            if host is None:
                raise Refusal(Finding("config.missing", "no host config: pass --host or set UNBAKE_HOST"))
            handler = _handler(name)
            if name == "init":
                result = handler({**params, "host": host})
            else:
                root = params.pop("project") or Path.cwd()
                cfg = config.load(root, host)
                effort.bind(cfg)
                result = handler(cfg, params)
                if findings := _refused(result):
                    code = 1
    except Refusal as refusal:
        code, findings = 1, list(refusal.findings)
    except Exception as exc:
        sys.stderr.write(traceback.format_exc())
        code, findings = 3, [Finding("internal.error", repr(exc))]
    return _emit(name, code, result, findings)

def _type(kind: str) -> Any:
    return click.Path(path_type=Path) if kind == "path" else (int if kind == "int" else str)

def _param(p: Json) -> click.Parameter:
    kind, flag = _type(p["type"]), f"--{p['name']}"
    if p["kind"] == "argument":
        return click.Argument([p["name"]], required=p["required"], nargs=-1 if p["multiple"] else 1, type=kind)
    if p["kind"] == "option":
        return click.Option([flag], required=p["required"], multiple=p["multiple"], type=kind, help=p["help"])
    return click.Option([flag], is_flag=True, help=p["help"])

def _group(outcome: dict[str, int], argv: Sequence[str]) -> click.Group:
    path = click.Path(path_type=Path)
    group = click.Group("unbake", params=[click.Option(["--host"], type=path, envvar="UNBAKE_HOST"),
                                        click.Option(["--project"], type=path)])
    for name, spec in config.load_resource("commands.toml")["command"].items():
        params = [_param(p) for p in spec["params"]]
        params.append(click.Option(["--host"], type=path, required=False, envvar="UNBAKE_HOST", help="host config"))
        if name != "init":
            params.append(click.Option(["--project"], type=path, required=False, help="project root, default cwd"))

        def callback(_name: str = name, **kwargs: Any) -> None:
            parent = click.get_current_context().parent.params
            for key in ("host", "project"):
                if key in kwargs and kwargs[key] is None:
                    kwargs[key] = parent.get(key)
            outcome["code"] = _run(_name, kwargs, argv)

        group.add_command(click.Command(name, params=params, callback=callback, help=spec["help"]))
    return group

def main(argv: Sequence[str] | None = None) -> int:
    human.attach(sys.stderr, sys.stderr.isatty())
    args = list(sys.argv[1:] if argv is None else argv)
    outcome: dict[str, int] = {"code": 0}
    try:
        _group(outcome, args).main(args=args, prog_name="unbake", standalone_mode=False)
    except click.exceptions.Exit as exit_:
        return int(exit_.exit_code)
    except click.ClickException as exc:
        sys.stderr.write(f"usage: {exc.format_message()}\n")
        return 2
    return outcome["code"]
