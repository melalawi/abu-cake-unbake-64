"""Catalogue header declarations and fold candidate declarations into their owners."""

import pickle
import posixpath
import re
from collections import defaultdict
from collections.abc import Collection
from copy import deepcopy

from pycparser import CParser, c_ast, c_generator
from pycparser.c_parser import ParseError

from unbake import effort, pool, store, types, view
from unbake.contracts import Finding, Refusal, Snapshot, SourceView, UnitSpec, digest


def _blank(text):
    return re.sub(r'/\*.*?\*/|//[^\n]*',
                  lambda m: re.sub(r'[^\n]', ' ', m[0]), text, flags=re.S)

def sources(snapshot: Snapshot, place: str = 'include', suffix: str = '.h') -> list[str]:
    root = snapshot.config.project.root
    paths = {p.relative_to(root).as_posix() for p in (root / place).rglob('*' + suffix)}
    paths.update(p for p in snapshot.overlays if p.startswith(place + '/') and p.endswith(suffix))
    return sorted(p for p in paths if p not in snapshot.overlays or snapshot.overlays[p] is not None)

def _quoted(content: bytes) -> tuple[str, ...]:
    return tuple(name.decode(errors="replace") for delimiter, name in view.directives(content) if delimiter == b'"')
def consumers(snapshot: Snapshot, changed: Collection[str]) -> tuple[str, ...]:
    """Every unit, of any group, that includes a changed header directly or through other headers. A quote include
    names a file beside the including one, else under include/ or src/; taking every one that exists
    over-approximates. The include graph is one fact of the snapshot: each file is read once, however many units ask,
    and its includes are kept by its content."""
    if not changed:
        return ()
    def graph() -> dict[str, set[str]]:
        known = {*sources(snapshot), *snapshot.layout.units, *changed}
        included_by: dict[str, set[str]] = defaultdict(set)
        for path in known:
            content = snapshot.peek(path) or b""
            for name in effort.memo(("quoted", digest(content)), lambda content=content: _quoted(content)):
                for base in (posixpath.dirname(path), "include", "src"):
                    if (target := posixpath.normpath(posixpath.join(base, name))) in known:
                        included_by[target].add(path)
        return included_by
    included_by = effort.memo(("included-by", snapshot.digest, tuple(sorted(changed))), graph)
    reached, todo = set(), list(changed)
    while todo:
        new = included_by.get(todo.pop(), set()) - reached
        reached |= new
        todo.extend(new)
    return tuple(path for path in reached if path in snapshot.layout.units)

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
                match = _DECLARATOR.search(part)
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
    r'^([ \t]*)(?!extern\b|typedef\b|return\b)((?:[\w]+[ \t*]+)+)(\w+)[ \t]*((?:\[[^\]]*\][ \t]*)*)[=;]', re.M)
_EXTERN = re.compile(r'^[ \t]*extern\b[^;{}]*;', re.M)
_SYNONYMS = {'f32': 'float', 'f64': 'double', 's8': 'signed char', 'u8': 'unsigned char', 's16': 'short',
             'u16': 'unsigned short', 's32': 'int', 'u32': 'unsigned int', 's64': 'long long',
             'u64': 'unsigned long long', 'const': '', 'volatile': '', 'extern': ''}

_FUNCTION = re.compile(r'^[A-Za-z_][\w \t*]*?\b(\w+)\s*\(([^;{}]*)\)\s*\{', re.M)

def _file_job(item) -> dict:
    text, source = item
    code = text.decode(errors='replace')
    clean = _blank(code)
    found = list(_DEFINITION.finditer(clean)) if source else []
    defs = [(f[3], clean.count('\n', 0, f.start()) + 1, f'{f[2]} @{f[4]}') for f in found if not f[1]]  # file scope
    declared = [(name, clean.count('\n', 0, f.start()) + 1, decl)
                for f in _EXTERN.finditer(clean) for name, _, decl in _declared(f[0])] if source else _declared(code)
    functions = [(f[1], clean.count('\n', 0, f.start()) + 1, ' '.join(f[0].split()).rstrip('{ '))
                 for f in _FUNCTION.finditer(clean)] if source else []
    return {'declared': declared, 'defined': [f[3] for f in found], 'defs': defs,
            'functions': functions, 'words': frozenset(re.findall(r'\w+', code))}

def _scanned(snapshot: Snapshot, paths: list[str]) -> list[dict]:
    return pool.map(snapshot.config, 'headers.catalog', _file_job,
                    [(snapshot.read(path), path.endswith('.c')) for path in paths], digest)

def landed(snapshot: Snapshot) -> dict[str, str]:
    """The head of every function a landed source defines (a fuzzy candidate is not landed)."""
    paths = sources(snapshot, 'src', '.c')
    return {name: text for path, scanned in zip(paths, _scanned(snapshot, paths), strict=True)
            if not path.startswith('src/fuzzy/') for name, _, text in scanned['functions']}

def externs(snapshot: Snapshot) -> dict[str, list[tuple[str, int, str]]]:
    """Per name the (path, line, type) of every extern statement a landed source makes of it."""
    paths = sources(snapshot, 'src', '.c')
    def produce() -> bytes:
        found = defaultdict(list)
        for path, scanned in zip(paths, _scanned(snapshot, paths), strict=True):
            for name, line, text in scanned['declared']:
                found[name].append((path, line, _type_of(text, name).replace('@', '').strip()))
        return pickle.dumps(dict(found))
    key = digest([(p, snapshot.read(p)) for p in paths])
    return pickle.loads(store.cached(snapshot.config, 'externs', key, produce))

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

_TYPE_WORDS = {'const', 'volatile', 'unsigned', 'signed', 'struct', 'union', 'enum', 'int', 'char', 'short', 'long',
               'void', 'float', 'double'}

def _shape(spelled: str) -> str:
    return re.sub(r'\[[^\]]*\]', '[]', _normal(spelled)).replace('static ', '').strip()

def _unnamed(spelled: str) -> str:
    """A function type without its parameter names: `@(s32 a, s32 *b)` and `@(s32, s32*)` are one type."""
    head, found, rest = spelled.partition('@(')
    if not found:
        return spelled
    kept = []
    for param in (p.strip() for p in rest.rpartition(')')[0].split(',')):
        named = re.search(r'(\w+)\s*$', param)
        kept.append(param[:named.start()] if named and named.start() and named[1] not in _TYPE_WORDS else param)
    return re.sub(r'\s+|\bvoid\b(?=\))|\b(?:struct|union|enum)\b', '', head + '@(' + ','.join(kept) + ')')

def _normal(spelled: str) -> str:
    return ' '.join(re.sub(r'\b\w+\b', lambda w: _SYNONYMS.get(w[0], w[0]), spelled).split())

def disagreements(snapshot: Snapshot, defined: Collection[str] = ()) -> tuple[dict[str, str], list[Finding]]:
    """The one type each declared name has where every landed declaration of it agrees (and no source defines it),
    and a finding per name whose declarations disagree, naming each declaration with its file and line. A function
    a landed source defines is the canonical type: a declaration that spells another type is a finding too, whose
    symptoms hold the definition ({path, line, signature}) and the count of other landed files that use the name."""
    with effort.stage('headers.disagreements'):
        paths = [*sources(snapshot), *sources(snapshot, 'src', '.c')]
        return pool.map(snapshot.config, 'headers.disagreements', _disagreements,
                        [(snapshot, paths, tuple(sorted(defined)))], _disagreements_key)[0]
def _disagreements_key(item: tuple) -> str:
    snapshot, paths, defined = item
    return digest(([(p, snapshot.read(p)) for p in paths], defined))
def _disagreements(item: tuple) -> tuple[dict[str, str], list[Finding]]:
    snapshot, paths, defined = item
    found: dict[str, list[tuple[str, int, str]]] = {}
    owned, definitions, variables = set(defined), {}, defaultdict(list)
    scans = _scanned(snapshot, paths)
    words = [scanned['words'] for scanned in scans]
    for path, scanned in zip(paths, scans, strict=True):
        owned.update(scanned['defined'])
        if not path.startswith('src/fuzzy/'):  # a fuzzy candidate is not landed
            definitions.update({name: (path, line, text) for name, line, text in scanned['functions']})
            for name, line, text in scanned['defs']:
                variables[name].append((path, line, text))
        for name, line, text in scanned['declared']:
            if not text.startswith('typedef') and not re.match(r'(?:struct|union|enum)\b[^;(]*\{', text):
                found.setdefault(name, []).append((path, line, text))
    agreed, findings = {}, []
    for name, rows in sorted(variables.items()):  # a definition spells one type, as every landed declaration does
        spelled = {_shape(t) for _, _, t in rows} | {_shape(_type_of(t, name)) for _, _, t in found.get(name, ())}
        if len(spelled) > 1:
            shown = (*(f"{name}: {p}:{n} {' '.join(t.replace('@', name).split())}" for p, n, t in rows),
                     *(f"{name}: {p}:{n} {t}" for p, n, t in found.get(name, ())))
            findings.append(Finding('types.conflict', f"{name} is defined or declared {len(spelled)} ways",
                                    path=rows[0][0], line=rows[0][1], unit=name, missing=shown, blocking=False,
                                    action='declare and define it with one type'))
    for name, rows in sorted(found.items()):
        if name in definitions:
            path, line, text = definitions[name]
            canonical = _unnamed(_normal(_type_of(text, name)))
            if canonical and any(_unnamed(_normal(_type_of(t, name))) != canonical for _, _, t in rows):
                shown = tuple(f"{name}: {p}:{n} {t}" for p, n, t in [definitions[name], *rows])
                excluded = {p for p, _, _ in rows} | {path}
                findings.append(Finding(
                    'types.conflict', f"{name} is defined as {text} but declared another way", path=path, line=line,
                    unit=name, missing=shown, blocking=False, action=f"declare it as its definition in {path} does",
                    symptoms={'definition': {'path': path, 'line': line, 'signature': text},
                              'consumers': sum(name in w and p not in excluded
                                               for p, w in zip(paths, words, strict=True))}))
            continue
        if name in owned:
            continue
        spelled = [_type_of(text, name) for _, _, text in rows]
        types = {_normal(s) for s in spelled}
        if len(types) == 1 and '' not in spelled:
            agreed[name] = spelled[0]
        else:
            shown = tuple(f"{name}: {path}:{line} {text}" for path, line, text in rows)
            findings.append(Finding('types.conflict', f"{name} is declared {len(types)} ways and defined nowhere",
                                    unit=name, missing=shown, blocking=False,
                                    action='declare it once, with one type, in a shared header'))
    return agreed, findings

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
        kind = _normal(_type_of(text, name))
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
                            _unnamed(_normal(_type_of(normal(entry['signature']), name))) !=
                            _unnamed(_normal(_type_of(text, name)))):
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
