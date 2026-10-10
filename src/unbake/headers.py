"""Catalogue header declarations and fold candidate declarations into their owners."""

import re
from collections.abc import Collection
from copy import deepcopy
from pathlib import Path

from pycparser import CParser, c_ast, c_generator
from pycparser.c_parser import ParseError

from unbake import effort, layout, pool, recipes, store, types
from unbake import view as _view
from unbake.contracts import Finding, Refusal, Snapshot, SourceView, UnitSpec, digest

_CODE = digest(Path(__file__).read_bytes())

def _blank(text):
    return re.sub(r'/\*.*?\*/|//[^\n]*',
                  lambda m: re.sub(r'[^\n]', ' ', m[0]), text, flags=re.S)

def sources(snapshot: Snapshot, place: str = 'include', suffix: str = '.h') -> list[str]:
    root = snapshot.config.project.root
    paths = {p.relative_to(root).as_posix() for p in (root / place).rglob('*' + suffix)}
    paths.update(p for p in snapshot.overlays if p.startswith(place + '/') and p.endswith(suffix))
    return sorted(p for p in paths if p not in snapshot.overlays or snapshot.overlays[p] is not None)

def _statements(text):
    clean = re.sub(r'^\s*#.*$', lambda m: re.sub(r'[^\n]', ' ', m[0]), _blank(text), flags=re.M)
    clean = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                   lambda m: re.sub(r'[^\n]', ' ', m[0]), clean)
    start, braces, parens = None, 0, 0
    for i, char in enumerate(clean):
        if start is None and not char.isspace():
            start = i
        parens += (char == '(') - (char == ')')
        braces += (char == '{') - (char == '}')
        if char == '}' and braces == 0 and start is not None:
            prefix = clean[start:clean.find('{', start, i)]
            if ')' in prefix and '=' not in prefix and not re.search(r'\b(struct|union|enum)\b', prefix):
                start = None
        if char == ';' and not braces and not parens and start is not None:
            yield start, i + 1, clean[start:i + 1]
            start = None

def _parts(text):
    start = depth = 0
    for i, char in enumerate(text):
        depth += (char in '([{') - (char in ')]}')
        if char == ',' and not depth:
            yield text[start:i]
            start = i + 1
    yield text[start:]

def _declared(text: str) -> list[tuple[str, int, str]]:
    """(name, line, statement) for every declaration one header makes."""
    rows = []
    for start, end, declaration in _statements(text):
        names = [f'{kind} {tag}' for kind, tag in re.findall(r'\b(struct|union|enum)\s+(\w+)\s*\{', declaration)]
        if re.search(r'\b(?:extern|typedef)\b', declaration):
            for part in _parts(declaration.rstrip(';')):
                match = re.search(r'\(\s*\*\s*(\w+)\s*\)|\b(\w+)\s*(?:\[[^]]*\]\s*)*(?:\([^{};]*\))?\s*$', part)
                if match:
                    names.append(match[1] or match[2])
        elif '{' not in declaration:
            match = re.search(r'\b(\w+)\s*\([^;]*\)\s*;', declaration)
            if match:
                names.append(match[1])
        rows.extend((name, text.count('\n', 0, start) + 1, ' '.join(_blank(text[start:end]).split())) for name in names)
    return rows

_DECLARATOR = re.compile(r'\(\s*\*\s*(\w+)\s*\)|\b(\w+)\s*(?:\[[^]]*\]\s*)*(?:\([^{};]*\))?\s*$')

# the names a source gives a value or storage: a declaration line without extern, whatever it sits in
_DEFINITION = re.compile(
    r'^[ \t]*(?!extern\b|typedef\b|return\b)(?:[\w]+[ \t*]+)+(\w+)[ \t]*(?:\[[^\]]*\][ \t]*)*[=;]', re.M)
_EXTERN = re.compile(r'^[ \t]*extern\b[^;{}]*;', re.M)
_SYNONYMS = {'f32': 'float', 'f64': 'double', 's8': 'signed char', 'u8': 'unsigned char', 's16': 'short',
             'u16': 'unsigned short', 's32': 'int', 'u32': 'unsigned int', 's64': 'long long',
             'u64': 'unsigned long long', 'const': '', 'volatile': '', 'extern': ''}

def _externs(text: str) -> list[tuple[str, int, str]]:
    """(name, line, statement) for every extern statement a source starts a line with: function bodies are not read."""
    clean = _blank(text)
    return [(name, clean.count('\n', 0, found.start()) + 1, statement)
            for found in _EXTERN.finditer(clean) for name, _, statement in _declared(found[0])]

def _file_job(item) -> dict:
    text, source = item
    code = text.decode(errors='replace')
    defined = _DEFINITION.findall(_blank(code)) if source else []
    return {'declared': (_externs if source else _declared)(code), 'defined': defined}
def _file_key(item) -> str:
    return digest((*item, _CODE))  # the file bytes, how it is read and the code that reads it

def _scanned(snapshot: Snapshot, paths: list[str]) -> list[dict]:
    return pool.map(snapshot.config, 'headers.catalog', _file_job,
                    [(snapshot.read(path), path.endswith('.c')) for path in paths], _file_key)

def catalog(snapshot: Snapshot, version: str) -> dict[str, tuple[str, int, str]]:
    with effort.stage('headers.catalog'):
        paths = sources(snapshot)
        result = {}
        for path, scanned in zip(paths, _scanned(snapshot, paths), strict=True):
            for name, line, text in scanned['declared']:
                result.setdefault(name, (path, line, text))
        return result

def _type_of(statement: str, name: str) -> str:
    """The type one declaration gives a name: its specifiers, the stars before it, and what follows it."""
    base = None
    for part in _parts(statement.rstrip(';')):
        match = _DECLARATOR.search(part)
        if match and base is None:
            base = part[:match.start()].rstrip('* ').strip()
        if match and name in (match[1], match[2]):
            stars = re.search(r'[*\s]*$', part[:match.start()])[0]
            return ' '.join(f"{base.removeprefix('extern')} {stars} {match[0].replace(name, '@', 1)}".split())
    return ''

def disagreements(snapshot: Snapshot, defined: Collection[str] = ()) -> tuple[dict[str, str], list[Finding]]:
    """The one type each declared name has where every landed declaration of it agrees (and no source defines it),
    and a finding per name whose declarations disagree, naming each declaration with its file and line."""
    with effort.stage('headers.disagreements'):
        paths = [*sources(snapshot), *sources(snapshot, 'src', '.c')]
        found: dict[str, list[tuple[str, int, str]]] = {}
        owned = set(defined)
        for path, scanned in zip(paths, _scanned(snapshot, paths), strict=True):
            owned.update(scanned['defined'])
            for name, line, text in scanned['declared']:
                if not text.startswith('typedef') and not re.match(r'(?:struct|union|enum)\b[^;(]*\{', text):
                    found.setdefault(name, []).append((path, line, text))
        agreed, findings = {}, []
        for name, rows in sorted(found.items()):
            if name in owned:
                continue
            spelled = [_type_of(text, name) for _, _, text in rows]
            types = {' '.join(re.sub(r'\b\w+\b', lambda w: _SYNONYMS.get(w[0], w[0]), s).split()) for s in spelled}
            if len(types) == 1 and '' not in spelled:
                agreed[name] = spelled[0]
            else:
                shown = tuple(f"{name}: {path}:{line} {text}" for path, line, text in rows)
                findings.append(Finding('types.conflict', f"{name} is declared {len(types)} ways and defined nowhere",
                                        unit=name, missing=shown, blocking=False,
                                        action='declare it once, with one type, in a shared header'))
        return agreed, findings

def context(snapshot: Snapshot, version: str) -> Path:
    with effort.stage('headers.context'):
        extra = types.context(snapshot)
        path = f'build/context/{version}-{digest(extra)[:16]}.c'
        source = ''.join(f'#include "{p.removeprefix("include/")}"\n' for p in sources(snapshot))
        output = snapshot.config.project.root / path
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            store.write(output, source.encode())
            # a plain snapshot reads the file just written, so the view key never carries the snapshot digest
            proposed = layout.overlay(snapshot, {path: source.encode()}) if snapshot.overlays else snapshot
            unit = UnitSpec(path, Path(path).suffix[1:], '', (), snapshot.config.project.toolchain,
                            {'add': [], 'omit': []})
            recipe = recipes.resolve(snapshot.config, unit, unit.options)
            result = _view.get(proposed, unit, version, recipe, lines=False)
            output = output.with_name(f'{version}-{digest((result.key, extra, _CODE))[:16]}.i')  # named by its content
            if not output.exists():
                text = result.text.rstrip('\n') + '\n'
                kept, dropped = _parseable(text, extra)
                store.write(output, (text + kept).encode())
                store.write(output.with_suffix('.dropped'), '\n'.join(dropped).encode())  # lines C cannot read
        except (Refusal, OSError) as error:
            raise Refusal(Finding('draft.context', reason=str(error), path=path)) from error
        return output
def _parseable(text: str, extra: str) -> tuple[str, list[str]]:
    """The type map lines a C parser reads after the headers' typedefs, and the ones it cannot."""
    try:
        ast = CParser().parse(normal(text))
    except ParseError as error:
        raise Refusal(Finding('draft.context', reason=f'the project headers do not parse: {error}')) from error
    names = {n.name for n in ast.ext if isinstance(n, c_ast.Typedef)}
    parser, lines, dropped = CParser(), [], []
    for line in extra.splitlines():  # each line on its own, after stubs of just the typedef names it uses
        words = dict.fromkeys(w for w in re.findall(r'\b[A-Za-z_]\w*\b', line) if w in names)
        try:
            parser.parse(normal(''.join(f'typedef int {w};\n' for w in words) + line + '\n'))
            lines.append(line)
        except ParseError:
            dropped.append(line)
    return '\n'.join(lines) + ('\n' if lines else ''), dropped

def normal(text: str) -> str:
    while match := re.search(r'\b__attribute__\s*\(', text):
        end, depth = match.end(), 1
        while end < len(text) and depth:
            depth += (text[end] == '(') - (text[end] == ')')
            end += 1
        text = text[:match.start()] + re.sub(r'[^\n]', ' ', text[match.start():end]) + text[end:]
    text = re.sub(r'^(# \d+ "[^"\n]*")(?: \d+)+[ \t]*$', r'\1', text, flags=re.M)  # the flags a marker may end with
    text = re.sub(r'\b(?:__extension__|__restrict)\b', '', text)
    return re.sub(r'\b(?:__inline__|__inline)\b', 'inline', text)

def _path(snapshot, coord):
    path = coord.file.replace('\\', '/') if coord and coord.file else ''
    path = path.removeprefix(snapshot.config.project.root.as_posix().rstrip('/') + '/')
    return re.sub(r'^(?:.*?/)?build/views/[0-9a-fA-F]{16}/', '', path).removeprefix('./')

_TAGGED = (c_ast.Struct, c_ast.Union, c_ast.Enum)
def _name(node):
    """The name a node declares; a struct, union or enum tag is spelled with its keyword, as C keeps tags apart."""
    if isinstance(node, c_ast.Decl) and not node.name and isinstance(node.type, _TAGGED):
        return f'{type(node.type).__name__.lower()} {node.type.name}' if node.type.name else None
    if isinstance(node, (c_ast.Decl, c_ast.Typedef)):
        return node.name or getattr(node.type, 'name', None)
    return None

def _canonical(node, typedefs):
    def expand(item, seen=frozenset()):
        if isinstance(item, c_ast.TypeDecl) and isinstance(item.type, c_ast.IdentifierType):
            names = item.type.names
            if len(names) == 1 and names[0] in typedefs and names[0] not in seen:
                replacement = expand(deepcopy(typedefs[names[0]]), seen | {names[0]})
                base = replacement
                while hasattr(base, 'type') and not isinstance(base, c_ast.TypeDecl):
                    base = base.type
                if hasattr(base, 'quals'):
                    base.quals = sorted(set(base.quals + item.quals))
                return replacement
        if isinstance(item, c_ast.TypeDecl):
            item.declname = None
        for field in item.__slots__:
            if field in ('coord', '__weakref__'):
                continue
            value = getattr(item, field)
            if isinstance(value, c_ast.Node):
                setattr(item, field, expand(value, seen))
            elif isinstance(value, list):
                setattr(item, field, [expand(v, seen) if isinstance(v, c_ast.Node) else v for v in value])
        return item
    return ' '.join(c_generator.CGenerator().visit(expand(deepcopy(node.type))).split())

def _candidate(node):
    if isinstance(node, c_ast.Typedef):
        return True
    return isinstance(node, c_ast.Decl) and 'static' not in node.storage and node.init is None and (
        'extern' in node.storage or isinstance(node.type, c_ast.FuncDecl) or
        (node.name is None and isinstance(node.type, (c_ast.Struct, c_ast.Union, c_ast.Enum))
         and (getattr(node.type, 'decls', None) is not None or getattr(node.type, 'values', None) is not None)))
def _clash(path: str, line: int, name: str, statement: str, entry: tuple[str, int, str]) -> list[Finding]:
    """A finding when a candidate types a name unlike its header: a function by return type, others by folded type."""
    def spelled(text: str) -> str:
        kind = re.sub(r'\b\w+\b', lambda w: _SYNONYMS.get(w[0], w[0]), _type_of(text, name))
        return ' '.join(re.sub(r'\[[^\]]*\]', '[]', kind.partition('@(')[0] if '@(' in kind else kind).split())
    mine, theirs = spelled(statement), spelled(entry[2])
    if not mine or not theirs or mine == theirs:
        return []
    return [Finding('headers.conflict', path=path, line=line, action='declare it the way the project does',
                    reason=f"{name}: candidate declares '{mine}', {entry[0]}:{entry[1]} declares '{theirs}'")]
def _included(text: str, paths: Collection[str]) -> str:
    """The text with an include of each path it lacks, after its last include."""
    wanted = sorted(p for p in paths if not re.search(r'^\s*#\s*include\s*[<"]' + re.escape(p) + r'[>"]', text, re.M))
    if not wanted:
        return text
    matches = (list(re.finditer(r'^\s*#\s*include[^\n]*(?:\n|$)', text, re.M))
               or list(re.finditer(r'^\s*#\s*define[^\n]*\n', text, re.M)))
    position = matches[-1].end() if matches else 0
    prefix = '\n' if position and text[position - 1] != '\n' else ''
    return text[:position] + prefix + ''.join(f'#include "{p}"\n' for p in wanted) + text[position:]
def _needs(declaration: str, known: dict[str, tuple[str, int, str]]) -> set[str]:
    """The headers that declare the types a declaration names: what a header that takes it in must include."""
    return {known[n][0].removeprefix('include/') for n in re.findall(r'\b(?:struct|union|enum) \w+|\w+', declaration)
            if n in known}

def fold(snapshot: Snapshot, unit: UnitSpec, view: SourceView) -> tuple[dict[str, bytes | None], tuple[Finding, ...]]:
    with effort.stage('headers.fold'):
        try:
            ast = CParser().parse(normal(view.text), filename=unit.path)
        except ParseError as error:
            match = re.search(r':(\d+)(?::\d+)?:', str(error))
            raise Refusal(Finding('headers.parse', reason=str(error), path=unit.path,
                                  line=int(match[1]) if match else 0)) from error
        typedefs = {n.name: n.type for n in ast.ext if isinstance(n, c_ast.Typedef)}
        visible = {}
        local = []
        for node in ast.ext:
            name = _name(node)
            if not name:
                continue
            if _path(snapshot, node.coord) == unit.path:
                local.append(node)
            else:
                visible.setdefault(name, node)
        source = snapshot.read(unit.path).decode()
        spans = list(_statements(source))
        offsets = [0] + [m.end() for m in re.finditer('\n', source)]
        grouped, includes, moved, findings = {}, set(), [], []
        known = catalog(snapshot, view.version)
        generator = c_generator.CGenerator()
        functions = types.load(snapshot)['function']
        for node in local:
            offset = offsets[node.coord.line - 1] + max(node.coord.column - 1, 0)
            span = next(((a, b) for a, b, _ in spans if a <= offset < b), None)
            if span is None:
                continue
            remove = False
            if _candidate(node):
                name = _name(node)
                other = visible.get(name)
                if other is not None:
                    left, right = _canonical(node, typedefs), _canonical(other, typedefs)
                    remove = left == right
                    if not remove:
                        location = f'{_path(snapshot, other.coord)}:{other.coord.line}'
                        findings.append(Finding('headers.conflict', path=unit.path, line=node.coord.line,
                            reason=f"{name}: local '{left}' vs {location} '{right}'"))
                elif name in known:
                    findings += _clash(unit.path, node.coord.line, name, generator.visit(node) + ';', known[name])
                    includes.add(known[name][0].removeprefix('include/'))
                    remove = True
                else:
                    entry = functions.get(name)
                    text = ' '.join(normal(generator.visit(node)).split())
                    if (entry and entry['evidence'] in ('authored', 'landed') and
                            isinstance(node.type, c_ast.FuncDecl) and
                            ' '.join(normal(entry['signature']).split()) != text):
                        findings.append(Finding('headers.conflict', path=unit.path,
                            reason=f"{name}: candidate declares {text}, the type map has {entry['signature']}"))
                    else:
                        moved.append((span, node))
                        remove = True
            grouped.setdefault(span, []).append((node, remove))
        for node in ast.ext:  # a definition the compiler would see beside a prototype of another return type
            if isinstance(node, c_ast.FuncDef) and _path(snapshot, node.coord) == unit.path and node.decl.name in known:
                findings += _clash(unit.path, node.coord.line, node.decl.name, generator.visit(node.decl),
                                   known[node.decl.name])
        owner = None
        writes = {}
        if moved:
            group = snapshot.layout.groups[unit.group]
            owner = f'include/{group.segment}/{group.name}.h'
            includes.add(owner.removeprefix('include/'))
            header = (snapshot.peek(owner) or b"").decode()
            if not header:
                guard = re.sub(r'[^A-Z0-9]', '_', f'UNBAKE_{group.segment}_{group.name}_H'.upper())
                header = f'#ifndef {guard}\n#define {guard}\n\n\n#endif\n'
            declarations = []
            for span, node in moved:
                entries = grouped[span]
                declarations.append(source[slice(*span)] if len(entries) == 1 else generator.visit(node) + ';')
            insertion = header.rfind('#endif')
            insertion = insertion if insertion >= 0 else len(header)
            header = (header[:insertion].rstrip() + '\n\n' + '\n'.join(declarations) + '\n\n' + header[insertion:])
            needs = [_needs(generator.visit(node), known) for _, node in moved]
            needed = set().union(*needs) - {owner.removeprefix('include/')}
            writes[owner] = _included(header, needed).encode()
        for (start, end), entries in sorted(grouped.items(), reverse=True):
            if any(remove for _, remove in entries):
                replacement = '\n'.join(generator.visit(n) + ';' for n, remove in entries if not remove)
                if not replacement:
                    first = source.rfind('\n', 0, start) + 1
                    last = source.find('\n', end)
                    last = len(source) if last < 0 else last + 1
                    if not source[first:start].strip() and not source[end:last].strip():
                        start, end = first, last
                source = source[:start] + replacement + source[end:]
        source = _included(source, includes)
        writes[unit.path] = source.encode()
        return writes, tuple(findings)
