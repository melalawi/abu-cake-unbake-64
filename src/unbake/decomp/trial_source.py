"""Map divergent draft instructions to compiler debug lines and C control spans."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp.explain import gcc_input
from unbake.decomp.trial_compare import Compare, align_words, words
from unbake.decomp.trial_compile import run_tool
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object


def control_context(text: str, line: int) -> str:
    """Name enclosing controls with their source boundaries, including nested controls."""
    clean = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: "\n" * match[0].count("\n"),
        text,
        flags=re.S,
    )
    tokens = list(
        re.finditer(
            r"^[ \t]*#\s*(?:if(?:def|ndef)?|elif|else|endif)\b[^\n]*|\b(?:for|while|do|if|switch)\b|[(){};]",
            clean,
            re.M,
        )
    )
    pairs: dict[int, int] = {}
    stacks: set[tuple[int, ...]] = {()}
    guards: list[tuple[set[tuple[int, ...]], set[tuple[int, ...]], bool]] = []
    for index, token in enumerate(tokens):
        value = token[0].strip()
        if value.startswith("#"):
            directive = re.match(r"#\s*(\w+)", value)
            assert directive is not None
            if directive[1] in ("if", "ifdef", "ifndef"):
                guards.append((stacks.copy(), set(), False))
            elif directive[1] in ("else", "elif") and guards:
                initial, completed, _ = guards[-1]
                completed.update(stacks)
                guards[-1] = initial, completed, directive[1] == "else"
                stacks = initial.copy()
            elif directive[1] == "endif" and guards:
                initial, completed, has_else = guards.pop()
                stacks.update(completed)
                if not has_else:
                    stacks.update(initial)
            continue
        if value in ("(", "{"):
            stacks = {(*stack, index) for stack in stacks}
        elif value in (")", "}"):
            updated = set()
            for stack in stacks:
                if stack and tokens[stack[-1]][0] == ("(" if value == ")" else "{"):
                    pairs[stack[-1]] = index
                    updated.add(stack[:-1])
                else:
                    updated.add(stack)
            stacks = updated
    contexts = []
    for index, token in enumerate(tokens):
        kind = token[0]
        if kind not in ("for", "while", "do", "if", "switch"):
            continue
        body = index + 1
        if kind != "do":
            if body >= len(tokens) or tokens[body][0] != "(" or body not in pairs:
                continue
            body = pairs[body] + 1
        while body < len(tokens) and tokens[body][0].lstrip().startswith("#"):
            body += 1
        if body >= len(tokens):
            continue
        end = pairs.get(body)
        if end is None:
            end = body
            while end < len(tokens) - 1 and tokens[end][0] != ";":
                end = min(pairs.get(end, end) + 1, len(tokens) - 1)
        start_line = clean.count("\n", 0, token.start()) + 1
        end_line = clean.count("\n", 0, tokens[end].end()) + 1
        if start_line <= line <= end_line:
            label = "loop" if kind in ("for", "while", "do") else "branch"
            contexts.append(f"{kind} {label} start line {start_line}; {label} end line {end_line}")
    return " | ".join(contexts)


def debug_locations(listing: str) -> dict[int, tuple[str, int]]:
    """Read objdump's debug file/line markers at exact instruction addresses."""
    locations = {}
    current: tuple[str, int] | None = None
    for line in listing.splitlines():
        marker = re.match(r"(.+):(\d+)(?: \(discriminator \d+\))?\s*$", line)
        if marker:
            current = marker[1], int(marker[2])
        instruction = re.match(r"\s*([0-9a-fA-F]+):\s+[0-9a-fA-F]{8}\s", line)
        if instruction and current:
            locations[int(instruction[1], 16)] = current
    return locations


def annotate_divergence(
    project: Project,
    policy: Policy,
    copied: Path,
    source: Path,
    version: str,
    work: Path,
    unit: Path,
    comparison: Compare,
) -> None:
    """Align compiler debug instructions with the scored object before reporting a source line."""
    first = next((i for i, line in enumerate(comparison.lines) if line.startswith("first divergence:")), None)
    if first is None:
        return
    detail = comparison.lines[first]
    offset = re.search(r"draft \+0x([0-9A-F]+)", detail)
    if offset is None:
        comparison.lines[first] += "; source unavailable for missing draft word"
        return
    try:
        compiler = project.compiler_for(source)
        if not compiler.id.startswith("gcc-"):
            comparison.lines[first] += "; source unavailable for this compiler's debug format"
            return
        expanded, flags = gcc_input(project, policy, copied, version, work, preserve_lines=True)
        input_path, assembly, debug = work / "debug.i", work / "debug.s", work / "debug.o"
        input_path.write_text(expanded)
        run_tool([str(compiler.cc), *flags, "-g", str(input_path), "-o", str(assembly)], work, "try")
        run_tool([str(policy.mips_as), "-EB", "-mips3", "--gdwarf-2", "-o", str(debug), str(assembly)], work, "try")
        actual, diagnostic = Object(unit), Object(debug)
        entry = next(
            symbol for symbols in actual.symbols.values() for symbol in symbols if symbol["name"] == source.stem
        )
        diagnostic_entry = next(
            symbol for symbols in diagnostic.symbols.values() for symbol in symbols if symbol["name"] == source.stem
        )
        actual_body = actual.content(entry["section"])[entry["value"] : entry["value"] + entry["size"]]
        diagnostic_body = diagnostic.content(diagnostic_entry["section"])[
            diagnostic_entry["value"] : diagnostic_entry["value"] + entry["size"]
        ]

        def normalized(body: bytes) -> list[int]:
            result = words(body)
            for index, word in enumerate(result):
                # Assemblers choose OR or ADDU for move and encode branch fixups differently.
                if word >> 26 == 0 and word & 63 in (0x21, 0x25) and word >> 16 & 31 == 0:
                    result[index] = word & ~63
                elif word >> 26 in (1, 4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17):
                    result[index] = word & ~0xFFFF
            return result

        wanted = (int(offset[1], 16) - entry["value"]) // 4
        mapped = None
        for tag, start, stop, debug_start, _ in align_words(normalized(actual_body), normalized(diagnostic_body), {}):
            if tag == "equal" and start <= wanted < stop:
                mapped = debug_start + wanted - start
                break
        if mapped is None:
            comparison.lines[first] += "; source unavailable: debug instruction cannot be aligned"
            return
        listing = run_tool([str(policy.mips_objdump), "-dl", str(debug)], work, "try")
        (work / "debug.asm").write_text(listing)
        address = diagnostic_entry["value"] + mapped * 4
        location = debug_locations(listing).get(address)
        if location is None:
            # Compiler prologues can precede the first debug line. Attribute only
            # that entry prefix to the source function declaration.
            entry_match = re.search(rf"\b{re.escape(source.stem)}\s*\([^;{{}}]*\)\s*{{", source.read_text())
            if entry_match is not None and (not debug_locations(listing) or address < min(debug_locations(listing))):
                number = source.read_text().count("\n", 0, entry_match.start()) + 1
                comparison.lines[first] += f"; source {source}:{number}; function entry"
            else:
                comparison.lines[first] += "; source unavailable: draft word has no debug line"
            return
        filename, number = location
        context = control_context(source.read_text(), number) if Path(filename) == source else ""
        comparison.lines[first] += f"; source {filename}:{number}"
        if context:
            comparison.lines[first] += "; " + context
    except (Held, OSError, ValueError, StopIteration) as error:
        comparison.lines[first] += f"; source unavailable: {error}"
