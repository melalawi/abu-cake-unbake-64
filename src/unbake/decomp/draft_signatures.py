"""Use explicit project definitions for referenced callees' ABI signatures."""

import re

from unbake.decomp.draft_context import preprocess_context
from unbake.layout import split
from unbake.layout.structs_parser import Parser
from unbake.layout.structs_types import SCALARS
from unbake.project.config import Held, Policy, Project


def declarations(project: Project, policy: Policy, version: str, assembly: str, context: str) -> str:
    callees = set(re.findall(r"\bjal\s+([A-Za-z_]\w*)", assembly))
    rows = split.functions(project, version)
    result = []
    for name in sorted(callees):
        if re.search(r"\b" + re.escape(name) + r"\s*\(", context):
            continue
        owners = [row for row in rows if name in row.aliases]
        if len(owners) != 1:
            continue
        source = project.src / (owners[0].path + ".c")
        if not source.is_file():
            continue
        # Select the same VERSION and include graph as the actual definition.
        try:
            expanded = preprocess_context(source, project, policy, version, source.stem)
        except Held:
            # An unavailable private include cannot invalidate an otherwise
            # supported caller. Its inferred signature still faces cc1 proof.
            continue
        clean = re.sub(r"/\*.*?\*/|//[^\n]*", " ", expanded, flags=re.S)
        pattern = (
            r"(?m)^[ \t]*(?P<return>(?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*\s*\**\s*)"
            + re.escape(name)
            + r"\s*\((?P<args>[^()]*)\)\s*\{"
        )
        matches = list(re.finditer(pattern, clean))
        if len(matches) != 1:
            continue
        parser = Parser(clean[: matches[0].start()])
        try:
            while parser.peek():
                if parser.peek() == "typedef":
                    parser.take()
                    parser.declaration(typedef=True)
                else:
                    parser.skip_external()
        except (Held, ValueError):
            continue

        def abi_type(value: str, *, parameter: bool, parser: Parser = parser) -> str | None:
            value = re.sub(r"\b(?:const|volatile|restrict|extern|static|inline)\b", "", value).strip()
            if parameter:
                value = re.sub(r"\b[A-Za-z_]\w*\s*(?:\[[^]]*\]\s*)*$", "", value).strip()
            if "*" in value or "[" in value:
                # Private aggregate names cannot escape their defining unit.
                # Keep known scalar pointees, otherwise only the pointer ABI.
                base = value.split("*", 1)[0].strip()
                if base not in SCALARS and base != "void":
                    base = "void"
                return base + " " + "*" * max(1, value.count("*"))
            if value == "void" or value in SCALARS:
                return value
            if value in parser.types:
                resolved = parser.type_name(value, ())
                return resolved if resolved in SCALARS else None
            return None

        match = matches[0]
        returned = abi_type(match["return"], parameter=False)
        args = match["args"].strip()
        parameters = [] if args in ("", "void") else args.split(",")
        types = ["..." if arg.strip() == "..." else abi_type(arg, parameter=True) for arg in parameters]
        if returned is not None and all(value is not None for value in types):
            result.append(f"{returned} {name}({', '.join(value for value in types if value is not None) or 'void'});")
    return "\n".join(result)
