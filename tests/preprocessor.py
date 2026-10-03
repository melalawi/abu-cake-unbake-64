"""Minimal include/conditional fixture expansion at the cpp process boundary."""

import re
from pathlib import Path


def expand(source, roots=(), macros=None, *, markers=False, origin="<stdin>", directives_only=False):
    macros = dict(macros or {})

    def read(text, home, origin="<stdin>"):
        text = text.replace("\\\n", "")
        text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
        result, active = [], [True]
        for line_number, line in enumerate(text.splitlines(), 1):
            match = re.match(r"\s*#\s*(\w+)\s*(.*)", line)
            if match:
                kind, value = match.groups()
                if kind in ("ifdef", "ifndef", "if"):
                    condition = (
                        value in macros
                        if kind != "if"
                        else bool(
                            (value in macros and macros[value] != "0")
                            or value == "1"
                            or (
                                re.fullmatch(r"defined\s*\(?\s*(\w+)\s*\)?", value)
                                and re.findall(r"\w+", value)[-1] in macros
                            )
                        )
                    )
                    if kind == "if" and value.startswith("!defined"):
                        condition = re.findall(r"\w+", value)[-1] not in macros
                    active.append(active[-1] and (not condition if kind == "ifndef" else condition))
                elif kind == "else":
                    active[-1] = active[-2] and not active[-1]
                elif kind == "endif":
                    active.pop()
                elif active[-1] and kind == "define":
                    key, _, replacement = value.partition(" ")
                    macros[key] = replacement or "1"
                elif active[-1] and kind == "undef":
                    macros.pop(value, None)
                elif active[-1] and kind == "include":
                    relative = value.strip('"<>')
                    candidates = [Path(relative), home / relative, *(Path(root) / relative for root in roots)]
                    path = next((path for path in candidates if path.is_file()), None)
                    if path is None:
                        raise ValueError(f"{relative}: missing fixture include")
                    if markers:
                        result.append(f'# 1 "{path}"')
                    result.append(read(path.read_text(), path.parent, str(path)))
                    if markers:
                        result.append(f'# {line_number + 1} "{origin}"')
                continue
            if active[-1]:
                if markers:
                    result.append(f'# {line_number} "{origin}"')
                for key, value in () if directives_only else macros.items():
                    if re.fullmatch(r"\w+", key):
                        line = re.sub(r"\b" + re.escape(key) + r"\b", lambda _, value=value: value, line)
                result.append(line)
        return "\n".join(result) + "\n"

    return read(source, Path.cwd(), origin)


def output(command, **kwargs):
    import subprocess

    cwd = Path(kwargs.get("cwd", Path.cwd()))
    roots = [cwd / flag[2:] for flag in command if flag.startswith("-I")]
    macros = dict(
        (flag[2:].partition("=")[0], flag.partition("=")[2] or "1") for flag in command if flag.startswith("-D")
    )
    source = kwargs.get("input")
    if source is None:
        source = Path(command[-1]).read_text()
    for index, flag in enumerate(command):
        if flag == "-include":
            source = (cwd / command[index + 1]).read_text() + "\n" + source
    result = expand(
        source,
        roots,
        macros,
        markers="-P" not in command,
        origin=command[-1] if "input" not in kwargs else "<stdin>",
        directives_only="-fdirectives-only" in command,
    )
    return subprocess.CompletedProcess(command, 0, result, "")
