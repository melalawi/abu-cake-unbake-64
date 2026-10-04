"""Explicit C declarations as target-ABI type seeds, without decompiler guesses."""

from __future__ import annotations

import copy
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

from pycparser import c_ast, c_generator, c_parser  # type: ignore[import-untyped]

from unbake.decomp.draft_context import ordered_headers
from unbake.decomp.header_declarations import attribute_source, declaration_source
from unbake.decomp.header_declarations import declarations as header_declarations
from unbake.layout.structs_parser import Parser
from unbake.project.config import Held, Policy, Project
from unbake.project.headers import include_headers
from unbake.project_tools import atomic as atomic_files
from unbake.typemap import storage

_BOUNDARY = "extern int __unbake_feedback_boundary;"
_C_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|^[ \t]*#[^\n]*|[A-Za-z_]\w*|\S', re.M)


def _outer_guard(text: str) -> str | None:
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
        lambda m: " " if m[0].startswith(("/*", "//")) else m[0],
        text,
        flags=re.S,
    )
    guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", text)
    if guard is None or not re.search(r"#\s*endif[^\n]*\s*\Z", text):
        return None
    depth = 0
    directives = list(re.finditer(r"^\s*#\s*(if|ifdef|ifndef|endif|else|elif)\b", text, re.M))
    for index, directive in enumerate(directives):
        if directive[1] in {"else", "elif"}:
            if depth == 1:
                return None
        else:
            depth += 1 if directive[1] != "endif" else -1
        if depth == 0 and index != len(directives) - 1:
            return None
    return guard[1] if depth == 0 else None


def _declaration_unit(source: str) -> str:
    """Keep function definitions' signatures, never parse their implementation.

    Proven GCC code may contain label addresses, computed goto or inline asm.
    None contributes to declaration evidence. Blank bodies without moving line
    markers; aggregate definitions and file-scope initializers remain intact.
    """
    tokens = list(_C_TOKEN.finditer(source))
    edits = []
    depth = parens = 0
    assigned = False
    previous = ""
    index = 0
    while index < len(tokens):
        token = tokens[index][0]
        if token.startswith("#"):
            index += 1
            continue
        if token == "{" and depth == 0 and previous == ")" and not assigned:
            begin = tokens[index].end()
            level = 1
            index += 1
            while index < len(tokens) and level:
                level += (tokens[index][0] == "{") - (tokens[index][0] == "}")
                index += 1
            if level:
                raise Held("solve", "types.declaration: unclosed function body")
            edits.append((begin, tokens[index - 1].start()))
            assigned = False
            previous = "}"
            continue
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            parens += (token == "(") - (token == ")")
            if not parens:
                assigned |= token == "="
                if token == ";":
                    assigned = False
        previous = token
        index += 1
    for begin, end in reversed(edits):
        body = source[begin:end]
        source = source[:begin] + re.sub(r"[^\n]", " ", body) + source[end:]
    return source


def clean(source: str, *, line_markers: bool = False) -> str:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", " ", source, flags=re.S)
    source = re.sub(r"^\s*#(?!\s*\d+\s+\")[^\n]*" if line_markers else r"^\s*#[^\n]*", "", source, flags=re.M)
    source = re.sub(r"\b(?:__extension__|__inline__|__inline|__restrict|restrict)\b", "", source)
    return attribute_source(source)


def headers(
    project: Project,
    policy: Policy | None,
    version: str,
    extra: Path | None = None,
    *,
    line_markers: bool = False,
    contents: dict[Path, str] | None = None,
) -> str:
    if contents is None:
        contents = {
            path: path.read_text()
            for path, _ in include_headers(project, exclude=lambda path: storage.generated(project, path))
            if not storage.generated(project, path)
        }
    if extra is None:
        from unbake.project.cache import remembered

        selection = (
            project.root,
            version,
            line_markers,
            None if policy is None else (str(policy.cpp), tuple(policy.cppflags)),
            project.compilers[project.default_compiler].cflags,
            project.include,
            project.version(version).macros,
            tuple(sorted(contents.items())),
        )
        return remembered(
            "typemap.headers",
            selection,
            lambda: _headers(project, policy, version, contents, None, line_markers=line_markers),
            keep=8,
        )
    return _headers(project, policy, version, contents, extra, line_markers=line_markers)


def _generated_context(project: Project) -> list[Path]:
    if not project.include:
        return []
    from unbake.layout import index

    return sorted(index.headers(project))


def _headers(
    project: Project,
    policy: Policy | None,
    version: str,
    contents: dict[Path, str],
    extra: Path | None,
    *,
    line_markers: bool,
    ordered: list[Path] | None = None,
    raw: bool = False,
    include_generated: bool = True,
) -> str:
    if ordered is None:
        ordered = ordered_headers(contents)
    if policy is None:
        # Raw guarded headers are useful to in-memory callers; other conditionals need cpp.
        for path, text in contents.items():
            if re.search(r"^\s*#\s*(?:if\b|elif\b|else\b)", text, re.M):
                raise Held("solve", f"types.declaration: {path}: policy.cpp required for conditional types")
        if extra is not None:
            if re.search(r"^\s*#\s*(?:if\b|ifdef\b|ifndef\b|elif\b|else\b)", extra.read_text(), re.M):
                raise Held("solve", f"types.declaration: {extra}: policy.cpp required for conditional C")
            text = "\n".join(contents[path] for path in ordered) + "\n"
            text += (_BOUNDARY + "\n" if raw else "") + extra.read_text()
            return text if raw else clean(text)
        if line_markers:
            return "\n".join(f'# 1 "{path}"\n' + clean(contents[path]) for path in ordered)
        return clean("\n".join(contents[path] for path in ordered))
    if not policy.cpp:
        raise Held("solve", "policy.cpp: required for typed header preprocessing")
    source = "".join(f'#include "{path}"\n' for path in ordered)
    if extra is not None:
        if include_generated:
            source += "".join(f'#include "{path}"\n' for path in _generated_context(project))
        if raw:
            source += _BOUNDARY + "\n"
        source += f'#include "{extra}"\n'
    command = _cpp_command(project, policy, version, extra=extra is not None, line_markers=line_markers)
    text = _preprocess(project, command, source)
    return text if raw else clean(text, line_markers=extra is not None or line_markers)


def _cpp_command(project: Project, policy: Policy, version: str, *, extra: bool, line_markers: bool) -> list[str]:
    flags: list[str] = []
    pending = iter(project.compilers[project.default_compiler].cflags)
    for flag in pending:
        if flag in ("-D", "-U", "-include", "-isystem"):
            value = next(pending, None)
            if value is None:
                raise Held("solve", f"compiler.cflags.{flag}: missing argument")
            flags.extend((flag, value))
        elif flag.startswith(("-D", "-U")):
            flags.append(flag)
    return [
        str(policy.cpp),
        *(f"-I{root}" for root in project.include),
        *(flag for flag in policy.cppflags if not line_markers or flag != "-P"),
        *flags,
        *(("-P",) if not extra and not line_markers else ()),
        "-x",
        "c",
        *(f"-D{macro}" for macro in project.version(version).macros),
        *(("-DUNBAKE_PROTOTYPES_H",) if extra else ()),
        "-",
    ]


def _preprocess(project: Project, command: list[str], source: str) -> str:
    try:
        result = subprocess.run(command, input=source, text=True, capture_output=True, cwd=project.root)
    except OSError as error:
        raise Held("solve", f"policy.cpp: {error}") from error
    if result.returncode:
        raise Held("solve", "types.declaration: " + result.stderr.strip())
    return str(result.stdout)


class _PublishedHeaders:
    """Preprocess the common includes once, then replay their final macro state.

    Each source still runs cpp independently, so its defines and undefines cannot
    affect another source. Include guards suppress already-emitted declarations.
    Stateful preprocessor extensions retain the ordinary full-unit path.
    """

    def __init__(self, project: Project, policy: Policy | None, scratch: Path, *, source_context: bool = False) -> None:
        self.project, self.policy, self.scratch = project, policy, scratch
        self.include_generated = not source_context
        self.contents = (
            {}
            if source_context
            else {
                path: path.read_text()
                for path, _ in include_headers(project, exclude=lambda path: storage.generated(project, path))
                if not storage.generated(project, path)
            }
        )
        self.ordered = ordered_headers(self.contents)
        texts = list(self.contents.values())
        generated = [] if source_context else _generated_context(project)
        texts.extend(path.read_text() for path in generated)
        self.replay = policy is not None and not any(
            re.search(r"__COUNTER__|^\s*#\s*pragma\b", text, re.M) for text in texts
        )
        self.guarded = {}
        consumed = dict(self.contents)
        consumed.update({path: path.read_text() for path in generated})
        for path, text in consumed.items():
            if guard := _outer_guard(text):
                self.guarded[path.resolve()] = guard
        self.prepared: dict[str, tuple[str, list[str]] | None] = {}
        self.macros: dict[str, dict[str, str]] = {}
        self.batch_headers: dict[Path, str] = {}
        self.batch_guards: dict[Path, str | None] = {}
        self.batch_effects: dict[
            Path, list[tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str] | None]]
        ] = {}
        self.batch_directives: dict[Path, list[tuple[str, str]] | None] = {}
        self.batch_includes: dict[tuple[Path, str], Path | None] = {}
        self.batch_resolved: dict[Path, Path] = {}
        self.batch_macro_names: dict[str, frozenset[str]] = {}
        self.batch_safe: dict[str, bool] = {}

    def source(self, version: str, source: Path) -> str | tuple[str, str]:
        project, policy = self.project, self.policy
        if self.replay and version not in self.prepared:
            assert policy is not None
            raw = _headers(
                project,
                policy,
                version,
                self.contents,
                source,
                line_markers=False,
                ordered=self.ordered,
                raw=True,
                include_generated=self.include_generated,
            )
            prefix, marker, _ = raw.partition(_BOUNDARY + "\n")
            if not marker:
                raise Held("solve", "types.declaration: missing preprocessor source boundary")
            prelude = "".join(f'#include "{path}"\n' for path in self.ordered)
            if self.include_generated:
                prelude += "".join(f'#include "{path}"\n' for path in _generated_context(project))
            command = _cpp_command(project, policy, version, extra=True, line_markers=False)
            macros = _preprocess(project, [*command[:-1], "-dM", "-"], prelude)
            # A provider must support cpp's macro dump, including fixture providers.
            if not macros.startswith("#define "):
                self.prepared[version] = None
            else:
                path = self.scratch / (version + ".macros.h")
                atomic_files.text(path, macros)
                self.macros[version] = {
                    match[1]: line
                    for line in macros.splitlines(keepends=True)
                    if (match := re.match(r"#define\s+([A-Za-z_]\w*)", line))
                }
                # Forced includes were already consumed while preparing the prefix.
                pending = iter(command)
                replay = []
                for flag in pending:
                    if flag == "-include":
                        next(pending)
                    else:
                        replay.append(flag)
                self.prepared[version] = prefix, [*replay[:-1], "-imacros", str(path), "-"]
        prepared = self.prepared.get(version)
        if prepared is None:
            return _headers(
                project,
                policy,
                version,
                self.contents,
                source,
                line_markers=False,
                ordered=self.ordered,
                raw=True,
                include_generated=self.include_generated,
            )
        prefix, command = prepared
        content = source.read_text()
        temporary = None
        if not re.search(r"^\s*#\s*(?:undef|define|include_next)\b", content, re.M):
            # A consumed, fully guarded include has no remaining effects. Avoid
            # opening megabytes of those headers just to rediscover their guards.
            def include(match: re.Match[str]) -> str:
                relative = match[1]
                paths = (source.parent / relative, *(root / relative for root in project.include))
                found = next((path.resolve() for path in paths if path.is_file()), None)
                if found in self.guarded:
                    return "\n" * match[0].count("\n")
                return match[0]

            content = re.sub(r'^\s*#\s*include\s*"([^"\n]+)"[^\n]*(?:\n|$)', include, content, flags=re.M)
            with tempfile.NamedTemporaryFile(
                mode="w", prefix=version + ".", suffix=".c", dir=self.scratch, delete=False
            ) as stream:
                stream.write(f'#line 1 "{source}"\n' + content)
                temporary = Path(stream.name)
            command = [*command[:-1], "-iquote", str(source.parent), "-"]
            source = temporary
        try:
            suffix = _preprocess(project, command, f'#include "{source}"\n')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return prefix, suffix

    def _batch_input(self, version: str, source: Path) -> tuple[str, set[str]] | None:
        """Enumerate macro effects, caching closures under their observed guard state."""
        macros = self.macros[version]
        macro_names = self.batch_macro_names.get(version)
        if macro_names is None:
            macro_names = frozenset(macros)
            self.batch_macro_names[version] = macro_names

        def directives(text: str) -> list[tuple[str, str]] | None:
            logical = re.sub(r"\\\r?\n", "", text)
            logical = re.sub(
                r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*',
                lambda m: " " if m[0].startswith(("/*", "//")) else m[0],
                logical,
                flags=re.S,
            )
            if re.search(r"\b(?:__COUNTER__|_Pragma|__INCLUDE_LEVEL__)\b", logical):
                return None
            rows = [(m[1], m[2].strip()) for m in re.finditer(r"^[ \t]*#[ \t]*(\w+)([^\n]*)", logical, re.M)]
            depth = 0
            for kind, _ in rows:
                if kind in {"if", "ifdef", "ifndef"}:
                    depth += 1
                elif kind in {"else", "elif", "endif"}:
                    if not depth:
                        return None
                    if kind == "endif":
                        depth -= 1
            return rows if depth == 0 else None

        def scan(
            path: Path, rows: list[tuple[str, str]] | None, inherited: set[str], visiting: frozenset[Path]
        ) -> tuple[set[str] | None, set[str]]:
            effects: set[str] = set()
            observed: set[str] = set()
            if rows is None:
                return None, observed
            current = set(inherited)
            for kind, argument in rows:
                if kind not in {
                    "define",
                    "undef",
                    "include",
                    "if",
                    "ifdef",
                    "ifndef",
                    "elif",
                    "else",
                    "endif",
                    "line",
                    "error",
                    "warning",
                }:
                    return None, observed
                if kind in {"define", "undef"}:
                    name = re.match(r"[A-Za-z_]\w*", argument)
                    if name is None or name[0] in {
                        "__LINE__",
                        "__FILE__",
                        "__BASE_FILE__",
                        "__DATE__",
                        "__TIME__",
                        "__TIMESTAMP__",
                        "__COUNTER__",
                        "__INCLUDE_LEVEL__",
                        "__has_include",
                    }:
                        return None, observed
                    effects.add(name[0])
                    current.add(name[0])
                elif kind == "include":
                    include = re.fullmatch(r'[<"]([^>"\n]+)[>"]', argument)
                    if include is None:
                        return None, observed
                    key = path.parent, argument
                    if key not in self.batch_includes:
                        roots = ([path.parent] if argument.startswith('"') else []) + list(self.project.include)
                        found = next((root / include[1] for root in roots if (root / include[1]).is_file()), None)
                        if found is None:
                            self.batch_includes[key] = None
                        else:
                            canonical = self.batch_resolved.get(found)
                            if canonical is None:
                                canonical = found.resolve()
                                self.batch_resolved[found] = canonical
                            self.batch_includes[key] = canonical
                    resolved = self.batch_includes[key]
                    if resolved is None:
                        return None, observed
                    child, guards = header(resolved, current, visiting)
                    observed.update(guards)
                    if child is None:
                        return None, observed
                    effects.update(child)
                    current.update(child)
            return effects, observed

        def header(path: Path, changed: set[str], visiting: frozenset[Path]) -> tuple[set[str] | None, set[str]]:
            entries = self.batch_effects.setdefault(path, [])
            for guards, context, defined, cached_effects in entries:
                if changed.intersection(guards) == context and guards.intersection(macro_names) == defined:
                    return None if cached_effects is None else set(cached_effects), set(guards)
            if path not in self.batch_headers:
                text = path.read_text()
                self.batch_guards[path] = _outer_guard(text)
                self.batch_directives[path] = directives(text)
                self.batch_headers[path] = text
            guard = self.batch_guards[path]
            observed = {guard} if guard is not None else set()
            if guard is not None and guard in macros and guard not in changed:
                effects: set[str] | None = set()
            elif path in visiting:
                return None, observed
            else:
                effects, nested = scan(path, self.batch_directives[path], changed, visiting | {path})
                observed.update(nested)
            entries.append(
                (
                    frozenset(observed),
                    frozenset(changed.intersection(observed)),
                    frozenset(observed.intersection(macro_names)),
                    None if effects is None else frozenset(effects),
                )
            )
            del entries[:-4]
            return effects, observed

        effects, _ = header(source, set(), frozenset())
        text = self.batch_headers[source]
        if effects is None:
            return None
        return text, effects

    def batch(self, version: str, sources: list[Path]) -> list[str | tuple[str, str] | Held]:
        """One cpp for a bounded batch, restoring its exact macro state per source.

        Literal closures enumerate all possible define/undef effects, including
        inactive branches and header guards. Stateful or unresolved inputs and
        failed framing use independent invocations, retaining per-source holds.
        """

        def individual(source: Path) -> str | tuple[str, str] | Held:
            try:
                return self.source(version, source)
            except Held as error:
                return error

        prepared = self.prepared.get(version)
        if prepared is None or len({source.parent for source in sources}) > 1:
            return [individual(source) for source in sources]
        if version not in self.batch_safe:
            self.batch_safe[version] = not any(
                re.search(r"__COUNTER__|_Pragma|__INCLUDE_LEVEL__", line)
                for line in self.macros.get(version, {}).values()
            )
        if not self.batch_safe[version]:
            return [individual(source) for source in sources]
        prefix, command = prepared
        inputs = [self._batch_input(version, source) for source in sources]
        selected = [index for index, row in enumerate(inputs) if row is not None]
        results: list[str | tuple[str, str] | Held] = ["" for _ in sources]
        if selected:
            changed = set().union(*(row[1] for index in selected if (row := inputs[index]) is not None))
            reset = "".join(f"#undef {name}\n" + self.macros[version].get(name, "") for name in sorted(changed))
            marker = "__unbake_feedback_unit_" + uuid4().hex + "_"
            units = []
            try:
                for number, index in enumerate(selected):
                    row = inputs[index]
                    assert row is not None
                    units.append(
                        f"extern int {marker}{number};\n" + reset + f'#line 1 "{sources[index]}"\n' + row[0] + "\n"
                    )
                units.append(f"extern int {marker}{len(selected)};\n")
                expanded = _preprocess(
                    self.project, [*command[:-1], "-iquote", str(sources[selected[0]].parent), "-"], "".join(units)
                )
                frames = re.split(
                    r"^[ \t]*extern[ \t]+int[ \t]+" + marker + r"(\d+)[ \t]*;[ \t]*$", expanded, flags=re.M
                )

                def padding(text: str) -> bool:
                    return not re.sub(r"^\s*#\s*(?:line\s+)?\d+[^\n]*", "", text, flags=re.M).strip()

                if (
                    len(frames) != 2 * len(selected) + 3
                    or [int(frame) for frame in frames[1::2]] != list(range(len(selected) + 1))
                    or not padding(frames[0])
                    or not padding(frames[-1])
                ):
                    raise Held("solve", "types.declaration: independent preprocessor framing required")
                for number, index in enumerate(selected):
                    results[index] = prefix, frames[2 * number + 2]
            except Held:
                selected = []
        accepted = set(selected)
        for index, source in enumerate(sources):
            if index not in accepted:
                results[index] = individual(source)
        return results


def _source_units(
    headers_batch: _PublishedHeaders, tasks: list[tuple[str, Path, str, dict[str, Any]]]
) -> Iterator[tuple[tuple[str, Path, str, dict[str, Any]], str | tuple[str, str] | Held]]:
    """Bound cpp concurrency and preserve receipt order for deterministic merging."""
    dummy = headers_batch.scratch / ".empty.c"
    atomic_files.text(dummy, "")
    for version in dict.fromkeys(task[2] for task in tasks):
        headers_batch.source(version, dummy)

    def preprocess(group: tuple[str, list[int]]) -> list[tuple[int, str | tuple[str, str] | Held]]:
        version, indices = group
        texts = headers_batch.batch(version, [tasks[index][1] for index in indices])
        return list(zip(indices, texts, strict=True))

    cores = min(12, getattr(headers_batch.policy, "cores", 1))
    with ThreadPoolExecutor(max_workers=cores) as pool:
        for start in range(0, len(tasks), 128):
            groups: dict[tuple[str, Path], list[int]] = {}
            for index in range(start, min(start + 128, len(tasks))):
                groups.setdefault((tasks[index][2], tasks[index][1].parent), []).append(index)
            results = {
                index: text
                for rows in pool.map(preprocess, [(version, indices) for (version, _), indices in groups.items()])
                for index, text in rows
            }
            for index in range(start, min(start + 128, len(tasks))):
                yield tasks[index], results[index]


class ProvenStructs(Mapping[str, Any]):
    """A shared immutable layout template with this receipt's provenance."""

    def __init__(self, template: dict[str, Any], provenance: dict[str, Any]) -> None:
        self.template, self.provenance = template, provenance

    def __len__(self) -> int:
        return len(self.template)

    def __iter__(self) -> Iterator[str]:
        return iter(self.template)

    def __getitem__(self, name: str) -> Any:
        return {**self.template[name], "provenance": self.provenance}


class _SeededParser(c_parser.CParser):  # type: ignore[misc]
    """Resume the file scope of an exact preprocessed prefix (pinned pycparser 3)."""

    def __init__(self, scope: dict[str, bool]) -> None:
        super().__init__()
        self.scope = scope

    def _parse_translation_unit_or_empty(self) -> Any:
        self._scope_stack = [self.scope.copy()]
        return super()._parse_translation_unit_or_empty()


class _FullDeclarationUnit(Exception):
    """A source changes the layout meaning of its shared header prefix."""


def _type(node: Any) -> str:
    # A named aggregate's body never enters an abstract type spelling. Prune
    # it before copying; callbacks can otherwise copy a whole shared layout.
    memo: dict[int, Any] = {}

    def names(value: Any) -> None:
        if isinstance(value, (c_ast.Struct, c_ast.Union)) and value.name:
            retained = copy.copy(value)
            retained.decls = None
            memo[id(value)] = retained
        else:
            for _, child in value.children():
                names(child)

    names(node)
    node = copy.deepcopy(node, memo)

    class Anonymous(c_ast.NodeVisitor):  # type: ignore[misc]
        def visit_TypeDecl(self, value: Any) -> None:
            value.declname = None
            self.generic_visit(value)

        def visit_Struct(self, value: Any) -> None:
            if value.name:
                value.decls = None

        visit_Union = visit_Struct

    Anonymous().visit(node)
    return " ".join(c_generator.CGenerator().visit(node).split())


def canonical(type_: str, aliases: dict[str, str]) -> str:
    previous = set()
    while type_ not in previous:
        previous.add(type_)
        changed = re.sub(
            r"\b(?:struct|union|enum)\s+[A-Za-z_]\w*|\b[A-Za-z_]\w*\b",
            lambda m: aliases.get(m[0], m[0]),
            type_,
        )
        if changed == type_:
            break
        type_ = changed
    type_ = re.sub(r"\s*\*\s*", " *", type_).strip()
    return {
        "signed": "int",
        "signed int": "int",
        "signed short": "short",
        "signed short int": "short",
        "signed long": "long",
        "signed long int": "long",
        "signed long long": "long long",
        "signed long long int": "long long",
        "unsigned": "unsigned int",
        "short int": "short",
        "unsigned short int": "unsigned short",
        "long long int": "long long",
        "unsigned long long int": "unsigned long long",
        "long int": "long",
        "unsigned long int": "unsigned long",
    }.get(type_, type_)


def unknown(type_: str) -> bool:
    """Decompiler placeholders specify machine widths, not semantic C types."""
    return bool(re.search(r"\bM2C_(?:UNK|UNKNOWN)\w*\b", type_))


def parameter_registers(params: list[dict[str, Any]], aliases: dict[str, str]) -> list[str | None]:
    """O32 first four words, including the leading floating-point register rule."""
    result: list[str | None] = []
    slot = 0
    floating_prefix = True
    for param in params:
        type_ = canonical(param["type"], aliases)
        width = 2 if type_ in ("double", "long long", "unsigned long long") else 1
        if width == 2:
            slot += slot % 2
        floating = type_ in ("float", "double")
        if floating and floating_prefix and len(result) < 2:
            result.append("f12" if not result else "f14")
        else:
            result.append(f"r{4 + slot}" if slot < 4 else f"stack{slot * 4}")
        floating_prefix &= floating
        slot += width
    return result


def extract(
    source: str,
    provenance: dict[str, Any],
    *,
    definitions: bool = False,
    owned_source: Path | None = None,
    authored_headers: set[Path] | None = None,
    _scope: dict[str, bool] | None = None,
    _prefix: dict[str, Any] | None = None,
    _parser: Any = None,
    _compact: bool = False,
    _contracts: bool = False,
) -> dict[str, Any]:
    source = clean(source, line_markers=owned_source is not None or authored_headers is not None)
    source = _declaration_unit(source)
    try:
        parser = _parser or (c_parser.CParser() if _scope is None else _SeededParser(_scope))
        tree = parser.parse(source)
    except Exception as error:
        raise Held("solve", f"types.declaration: {provenance}: {error}") from error
    incoming = {node.name: _type(node.type) for node in tree.ext if isinstance(node, c_ast.Typedef)}
    aliases = {} if _prefix is None else _prefix["aliases"] if _compact and not incoming else dict(_prefix["aliases"])
    aliases.update(incoming)
    shared_typedefs = (
        _prefix["shared_typedefs"]
        if _prefix is not None and _compact and not incoming
        else dict((_prefix or {}).get("shared_typedefs", {}))
    )
    shared_typedefs.update(
        (node.name, incoming[node.name])
        for node in tree.ext
        if isinstance(node, c_ast.Typedef)
        and (owned_source is None or (node.coord.file and node.coord.file != str(owned_source)))
    )
    prefix_structs = {} if _prefix is None else _prefix["structs"]
    complete_layout = False
    if _prefix is not None:
        additions = []
        overrides = bool(incoming and _prefix["unknown"])
        for name, type_ in incoming.items():
            if name in _prefix["aliases"]:
                if type_ != _prefix["aliases"][name]:
                    overrides = True
                continue
            target = canonical(type_, aliases)
            aggregate = re.fullmatch(r"(?:struct|union) (\w+)", target)
            if aggregate is not None:
                additions.append((aggregate[1], name))
            elif target.startswith(("struct {", "union {", "enum ")):
                overrides = True
        if overrides:
            variants = _prefix.setdefault("layout_overrides", {})
            selection = tuple(incoming.items())
            if selection not in variants:
                rows, unknown = _layout_records(_prefix["layout_source"] + source, {}, aliases)
                if unknown:
                    raise _FullDeclarationUnit
                variants[selection] = rows
            prefix_structs = variants[selection]
            complete_layout = True
        elif additions:
            variants = _prefix.setdefault("layout_aliases", {})
            selection = tuple(additions)
            if selection not in variants:
                changed = dict(prefix_structs)
                for tag, name in additions:
                    if tag in changed:
                        row = changed[tag]
                        changed[tag] = {**row, "aliases": [*row["aliases"], name]}
                variants[selection] = changed
            prefix_structs = variants[selection]
    result: dict[str, Any] = {
        "functions": {},
        "globals": {},
        "structs": {},
        "arrays": {},
        "aliases": aliases,
        "shared_typedefs": shared_typedefs,
        "unknown": [],
    }
    if _prefix is not None:
        for kind in ("functions", "globals", "arrays", "structs") if _contracts else ("functions", "structs"):
            if kind != "structs" or not _compact:
                template = prefix_structs if kind == "structs" else _prefix[kind]
                result[kind] = {name: {**row, "provenance": provenance} for name, row in template.items()}
        result["unknown"] = [] if complete_layout else list(_prefix["unknown"])
        if _compact:
            result["structs"] = ProvenStructs(prefix_structs, provenance)
            result["authored_structs"] = []
    if authored_headers is not None:
        authored_names: set[str] = set()

        def authored(node: Any) -> bool:
            return bool(node.coord and node.coord.file and Path(node.coord.file).resolve() in authored_headers)

        class Homes(c_ast.NodeVisitor):  # type: ignore[misc]
            def visit_Struct(self, node: Any) -> None:
                if node.name and node.decls and authored(node):
                    authored_names.add(node.name)
                self.generic_visit(node)

            visit_Union = visit_Struct

            def visit_Typedef(self, node: Any) -> None:
                base = node.type.type if isinstance(node.type, c_ast.TypeDecl) else None
                if isinstance(base, (c_ast.Struct, c_ast.Union)) and not base.name and base.decls and authored(node):
                    authored_names.add(node.name)
                self.generic_visit(node)

        Homes().visit(tree)
        result["authored_structs"] = sorted(authored_names)
    generator = c_generator.CGenerator()
    for node in tree.ext:
        definition = isinstance(node, c_ast.FuncDef)
        declaration = node.decl if definition else node
        if not isinstance(declaration, c_ast.Decl) or not declaration.name:
            continue
        if isinstance(declaration.type, c_ast.FuncDecl):
            if "static" in declaration.storage or (definitions and not definition and not _contracts):
                continue
            params: list[dict[str, Any]] = []
            variadic = False
            arguments = declaration.type.args
            if arguments is not None:
                for param in arguments.params:
                    if isinstance(param, c_ast.EllipsisParam):
                        variadic = True
                    elif _type(param.type) != "void":
                        params.append({"name": param.name or f"arg{len(params)}", "type": _type(param.type)})
            known_arity = arguments is not None
            result["functions"][declaration.name] = {
                "return": _type(declaration.type.type),
                "params": params,
                "variadic": variadic,
                "arity_known": known_arity,
                "prototype": generator.visit(declaration) + ";",
                "registers": parameter_registers(params, aliases),
                "provenance": provenance,
            }
        elif "static" not in declaration.storage and (
            _contracts or not definitions or (owned_source is not None and declaration.coord.file == str(owned_source))
        ):
            type_ = _type(declaration.type)
            declaration = copy.deepcopy(declaration)
            declaration.init = None
            result["globals"][declaration.name] = {
                "type": type_,
                "provenance": provenance,
                "declaration": "extern " + generator.visit(declaration).removeprefix("extern ") + ";",
            }
            if isinstance(declaration.type, c_ast.ArrayDecl):
                result["arrays"][declaration.name] = {
                    "type": _type(declaration.type.type),
                    "extent": generator.visit(declaration.type.dim) if declaration.type.dim is not None else None,
                    "provenance": provenance,
                }
    if not complete_layout:
        rows, unknown = _layout_records(source, provenance, aliases)
        if _prefix is not None and (unknown or rows):
            raise _FullDeclarationUnit
        for name, row in rows.items():
            result["structs"][name] = row
        result["unknown"].extend(unknown)
    return result


def _layout_typedefs(declaration: str, aliases: dict[str, str]) -> dict[str, str]:
    """Keep the typedef prerequisites of a retained aggregate's C spelling."""
    # Resolve prerequisite spellings so a pointer callback can precede the
    # aggregate whose fields use it, without introducing a typedef cycle.
    return {
        name: canonical(aliases[name], aliases)
        for name in sorted(header_declarations(declaration).uses)
        if name in aliases
    }


def _layout_records(
    source: str, provenance: dict[str, Any], aliases: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    records: dict[str, Any] = {}
    unknown = []
    layout_source = clean(source)
    try:
        for layout in Parser(layout_source).parse():
            if not layout.fields:
                continue
            declaration = layout_source[layout.start : layout.end] + ";"
            records[layout.name] = {
                "type": f"{layout.kind} {layout.name}",
                "size": layout.size,
                "alignment": layout.alignment,
                "aliases": list(layout.aliases),
                "declaration": declaration,
                "typedefs": _layout_typedefs(declaration, aliases),
                "fields": [
                    {"name": f.name, "type": f.type, "offset": f.offset, "size": f.size, "extent": list(f.extent)}
                    for f in layout.fields
                ],
                "provenance": provenance,
            }
    except Held as error:
        unknown.append("types.layout: " + error.reason)
    return records, unknown


def _portable_signatures(seed: dict[str, Any], shared_aliases: dict[str, str]) -> list[str]:
    """A generated header cannot refer to a typedef private to one C source."""
    local = {name: type_ for name, type_ in seed["aliases"].items() if shared_aliases.get(name) != type_}
    rewritten: list[str] = []
    if not local:
        return rewritten
    for name, record in seed["functions"].items():
        types = [record["return"], *(param["type"] for param in record["params"])]
        expanded = [canonical(type_, local) for type_ in types]
        if expanded == types:
            continue
        params = [declarator(type_, param["name"]) for type_, param in zip(expanded[1:], record["params"], strict=True)]
        if record["variadic"]:
            params.append("...")
        arguments = ", ".join(params) or ("void" if record["arity_known"] else "")
        storage_class = re.match(r"(?:(?:extern|static|inline)\s+)+", record["prototype"])
        prefix = "" if storage_class is None else storage_class[0]
        record["prototype"] = prefix + declarator(expanded[0], name + "(" + arguments + ")") + ";"
        rewritten.append(record["prototype"])
        # Width placeholders remain semantic unknowns even though their header
        # carrier uses the actual declared machine-width type.
        if not unknown(types[0]):
            record["return"] = expanded[0]
        record["params"] = [
            {**param, "type": type_ if unknown(param["type"]) else expanded_type}
            for param, type_, expanded_type in zip(record["params"], types[1:], expanded[1:], strict=True)
        ]
    return rewritten


class _PublishedDeclarations:
    """Share header parsing without sharing a source's preprocessor or C scope."""

    def __init__(self, *, contracts: bool = False) -> None:
        self.contracts = contracts
        self.prefixes: dict[str, tuple[str, dict[str, bool], dict[str, Any]]] = {}
        self.prefix_errors: dict[str, tuple[str, str]] = {}
        self.sources: dict[tuple[str, str], dict[str, Any]] = {}
        self.units: dict[str, str] = {}

    def extract(
        self, text: str | tuple[str, str], provenance: dict[str, Any], source: Path, *, compact: bool = False
    ) -> dict[str, Any]:
        if isinstance(text, tuple):
            prefix, suffix = text
        else:
            prefix, marker, suffix = text.partition(_BOUNDARY + "\n")
            if not marker:
                raise Held("solve", "types.declaration: missing preprocessor source boundary")
        if prefix in self.prefix_errors:
            phase, reason = self.prefix_errors[prefix]
            raise Held(phase, reason)
        cached = self.prefixes.get(prefix)
        if cached is None:
            cleaned = clean(prefix, line_markers=True)
            parser = c_parser.CParser()
            try:
                seed = extract(
                    cleaned,
                    {},
                    definitions=True,
                    owned_source=Path("__unbake_header_prefix__"),
                    _parser=parser,
                    _contracts=self.contracts,
                )
            except Held as error:
                # The exact prefix fails independently of every source suffix.
                # Preflight must retain each source's verdict without parsing
                # the same broken headers hundreds of times. Store only text,
                # never tracebacks that retain a partially parsed header AST.
                self.prefix_errors[prefix] = error.phase, error.reason
                raise
            seed["shared_typedefs"] = seed["aliases"]
            seed["layout_source"] = cleaned
            cached = cleaned, parser._scope_stack[0].copy(), seed
            self.prefixes[prefix] = cached
            while len(self.prefixes) > 4:
                del self.prefixes[next(iter(self.prefixes))]
        cleaned, scope, seed = cached
        unit = self.units.get(suffix)
        if unit is None:
            unit = _declaration_unit(clean(suffix, line_markers=True))
            self.units[suffix] = unit
            while len(self.units) > 8:
                del self.units[next(iter(self.units))]
        suffix = unit
        source_key = None
        if not re.search(r'^\s*#\s*\d+\s+"', suffix, re.M):
            # Without line markers, the existing ownership rule excludes source
            # globals. Resident literal storage and function bodies therefore
            # cannot change these declaration facts across containing versions.
            interface = re.sub(
                r"\bconst\s+(?:float|double|unsigned\s+(?:int|char|short)|int|char|short|long)\s+"
                r"unbake_rodata_[0-9A-Fa-f]+_[0-9A-Fa-f]+[^;]*;",
                "",
                suffix,
            )
            source_key = prefix, re.sub(r"\{\s*\}", "{}", interface).rstrip()
            previous = self.sources.get(source_key)
            if previous is not None:
                return _receipt_seed(previous, provenance, compact=compact)
        # New aggregate definitions need the full layout context. Anonymous
        # extern declarations do not define named layouts and stay incremental.
        try:
            result = extract(
                suffix,
                provenance,
                definitions=True,
                owned_source=source,
                _scope=scope,
                _prefix=seed,
                _compact=compact,
                _contracts=self.contracts,
            )
        except _FullDeclarationUnit:
            result = extract(
                cleaned + suffix, provenance, definitions=True, owned_source=source, _contracts=self.contracts
            )
        if result["shared_typedefs"] is not seed["aliases"]:
            result["shared_typedefs"] = {**seed["aliases"], **result["shared_typedefs"]}
        prototypes = _portable_signatures(result, seed["aliases"])
        if prototypes:
            try:
                _SeededParser(scope).parse("\n".join(prototypes))
            except Exception as error:
                raise Held("solve", f"types.declaration: {provenance}: emitted prototype: {error}") from error
        if (
            source_key is not None
            and not result["globals"]
            and not result["arrays"]
            and result["unknown"] == seed["unknown"]
        ):
            self.sources[source_key] = result
            while len(self.sources) > 8:
                del self.sources[next(iter(self.sources))]
        return result


def _receipt_seed(seed: dict[str, Any], provenance: dict[str, Any], *, compact: bool) -> dict[str, Any]:
    result = {**seed}
    for kind in ("functions", "globals", "arrays"):
        result[kind] = {name: {**row, "provenance": provenance} for name, row in seed[kind].items()}
    structs = seed["structs"]
    template = structs.template if isinstance(structs, ProvenStructs) else structs
    if compact:
        result["structs"] = ProvenStructs(template, provenance)
        result["authored_structs"] = []
    else:
        result["structs"] = {name: {**row, "provenance": provenance} for name, row in template.items()}
        result.pop("authored_structs", None)
    return result


def consumed_contracts(seed: dict[str, Any], text: str) -> dict[str, Any]:
    """An included header is not proof of its unused inferred declarations."""
    needed = set(re.findall(r"\b[A-Za-z_]\w*\b", declaration_source(text)))
    result = {**seed}
    for kind in ("functions", "globals", "arrays"):
        result[kind] = {name: row for name, row in seed[kind].items() if name in needed}
        for row in result[kind].values():
            needed.update(
                re.findall(
                    r"\b[A-Za-z_]\w*\b",
                    row.get("prototype", row.get("declaration", row.get("type", ""))),
                )
            )
    layouts = {}
    changed = True
    while changed:
        changed = False
        for name in list(needed):
            if name in seed["aliases"]:
                added = set(re.findall(r"\b[A-Za-z_]\w*\b", seed["aliases"][name])) - needed
                needed.update(added)
                changed |= bool(added)
        for name, row in seed["structs"].items():
            if name not in layouts and (name in needed or set(row.get("aliases", ())) & needed):
                layouts[name] = row
                needed.update(re.findall(r"\b[A-Za-z_]\w*\b", row["declaration"]))
                changed = True
    result["structs"] = layouts
    result["aliases"] = {name: row for name, row in seed["aliases"].items() if name in needed}
    result["shared_typedefs"] = {name: row for name, row in seed["shared_typedefs"].items() if name in needed}
    if "authored_structs" in seed:
        result["authored_structs"] = [name for name in seed["authored_structs"] if name in layouts]
    return result


def published_sources(project: Project) -> list[tuple[str, Path, str]]:
    """Use explicit published C ownership, never arbitrary scratch C files."""
    from unbake.layout import split as inventory

    return sorted(
        {
            (row.name, project.src / (row.path + ".c"), version)
            for version in project.versions
            for row in inventory.functions(project, version)
            if row.kind == "c"
        }
    )


def collect(project: Project, policy: Policy | None) -> list[dict[str, Any]]:
    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".declarations-", dir=project.build) as temporary:
        return _collect(project, policy, Path(temporary))


def validate_sources(
    project: Project, policy: Policy | None, entries: list[tuple[str, Path, tuple[str, ...]]]
) -> dict[str, str]:
    """Refuse declaration errors in the staged batch before proof/publication."""
    if not entries:
        return {}
    project.build.mkdir(parents=True, exist_ok=True)
    refused = {}
    with tempfile.TemporaryDirectory(prefix=".declarations-", dir=project.build) as temporary:
        # Admission follows the compiler's actual source imports, including
        # transitive providers and forced includes. Analysis imports every
        # authored header and the generated umbrella; an unrelated provider's
        # failure there must not veto the batch. Imported declarations remain
        # in each suffix so the existing per-source refusal owns the hold.
        headers_batch = _PublishedHeaders(project, policy, Path(temporary), source_context=True)
        published = _PublishedDeclarations()
        tasks: list[tuple[str, Path, str, dict[str, Any]]] = [
            (function, source, version, {}) for function, source, versions in entries for version in versions
        ]
        for (function, source, version, _), text in _source_units(headers_batch, tasks):
            if function in refused:
                continue
            try:
                if isinstance(text, Held):
                    raise text
                published.extract(
                    text, {"kind": "proven", "function": function, "version": version}, source, compact=True
                )
                published.sources.clear()
            except Held as error:
                refused[function] = error.reason
    return refused


def _collect(project: Project, policy: Policy | None, scratch: Path) -> list[dict[str, Any]]:
    seeds = []
    declared: dict[str, dict[str, Any]] = {}
    headers_batch = _PublishedHeaders(project, policy, scratch)
    published = _PublishedDeclarations()
    authored = {
        path.resolve() for root in project.include for path in root.rglob("*.h") if not storage.generated(project, path)
    }
    for version in project.versions:
        header_text = _headers(
            project, policy, version, headers_batch.contents, None, line_markers=True, ordered=headers_batch.ordered
        )
        provenance = {"kind": "declared", "version": version, "sha256": storage.digest(header_text.encode())}
        seed = declared.get(header_text)
        if seed is None:
            seed = extract(header_text, provenance, authored_headers=authored)
            declared[header_text] = seed
        else:
            seed = {**seed}
            for kind in ("functions", "globals", "structs", "arrays"):
                seed[kind] = {name: {**row, "provenance": provenance} for name, row in seed[kind].items()}
        seeds.append(seed)
    from unbake.typemap import declaration_evidence

    components = declaration_evidence.feedback_components(project)
    if components:
        exports = set().union(*(header_declarations(text).declared for text in components.values()))
        # Generated layouts are part of the prefix, while extern evidence is
        # retained at declared confidence (never promoted to an exact C proof).
        extra = scratch / "declaration_evidence.c"
        from unbake.layout import redeclarations
        from unbake.typemap import split
        from unbake.typemap.declaration_evidence import _body

        provided_layouts = {}
        for path in _generated_context(project):
            for statement in split.statements(_body(path.read_text())):
                for name in re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", declaration_source(statement)):
                    provided_layouts[name] = statement
        supplemental = []
        for body in components.values():
            for statement in split.statements(body):
                definitions = set(re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", declaration_source(statement)))
                duplicates = definitions & provided_layouts.keys()
                for name in duplicates:
                    if redeclarations.normalized(statement) != redeclarations.normalized(provided_layouts[name]):
                        raise Held(
                            "types",
                            f"declaration_evidence.{name}: local:\n{statement}\nshared:\n{provided_layouts[name]}",
                        )
                if not duplicates:
                    supplemental.append(statement)
        atomic_files.text(extra, "\n".join(supplemental))
        evidence: dict[str, dict[str, Any]] = {}
        for version in project.versions:
            provenance = {
                "kind": "declared",
                "version": version,
                "source": "declaration_evidence",
                "sha256": storage.digest(extra.read_bytes()),
            }
            # Evidence imports the generated context too. Preserve definition
            # homes so a canonical layout reused after a rename remains a
            # generated provider, with its split dependencies intact.
            text = headers(project, policy, version, extra, line_markers=True)
            template = evidence.get(text)
            if template is None:
                template = extract(text, provenance, authored_headers=authored)
                evidence[text] = template
            seed = _receipt_seed(template, provenance, compact=False)
            if "authored_structs" in template:
                seed["authored_structs"] = template["authored_structs"]
            for kind in ("functions", "globals", "arrays"):
                seed[kind] = {name: row for name, row in seed[kind].items() if name in exports}
            # Explicit declaration evidence retains complete layouts even when
            # no mapped instruction currently uses them. Their output homes
            # are generated, but their confidence remains authored/declared.
            declared_layouts = {
                name
                for text in components.values()
                for name in re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", declaration_source(text))
            }
            seed["authored_structs"] = sorted(set(seed["authored_structs"]) | declared_layouts)
            seeds.append(seed)
    # C intervals in the ROM layout are already published matches, including
    # including every source in src/. Import their actual compiler context;
    # an unrelated generated umbrella must not redefine their local contracts.
    contracts = _PublishedDeclarations(contracts=True)
    source_headers = _PublishedHeaders(project, policy, scratch, source_context=True)
    tasks: list[tuple[str, Path, str, dict[str, Any]]] = [
        (function, source, version, {}) for function, source, version in published_sources(project)
    ]
    for (function, source, version, _), contract_text in _source_units(source_headers, tasks):
        if isinstance(contract_text, Held):
            raise contract_text
        provenance = {
            "kind": "published",
            "function": function,
            "version": version,
            "source": str(source.relative_to(project.root)),
            "sha256": storage.file_digest(source),
        }
        contract = contracts.extract(contract_text, provenance, source, compact=True)
        seeds.append(consumed_contracts(contract, source.read_text()))
        # The definition owns the function contract; imported prototypes are
        # dependencies, and cannot override a ROM-proven definition elsewhere.
        owned = published.extract(contract_text, {**provenance, "kind": "proven"}, source, compact=True)
        owned["functions"] = {name: row for name, row in owned["functions"].items() if name == function}
        seeds.append(owned)
    return seeds


def declarator(type_: str, name: str) -> str:
    """Insert a name in an abstract C type, including arrays and function pointers."""
    if "(*" in type_ or re.search(r"\(\s*\*", type_):
        return re.sub(r"(\(\s*\*[^)]*)(\))", rf"\g<1>{name}\2", type_, count=1)
    pointer_array = re.fullmatch(r"(.+?)\s*(\[[^\]]*\](?:\s*\[[^\]]*\])*)\s*(\*+)", type_)
    if pointer_array:
        base, dimensions, stars = pointer_array.groups()
        return f"{base.rstrip()} ({stars}{name}){dimensions}"
    array = type_.find("[")
    if array >= 0:
        return type_[:array].rstrip() + " " + name + type_[array:]
    return type_ + " " + name
