"""Tests for headers: catalog, fold and the type-map aware context."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unbake import headers, layout, recipes, types
from unbake import view as view_module
from unbake.contracts import Refusal

HEADER = (
    "#ifndef G\n#define G\ntypedef int s32;\nextern s32 gCount;\nvoid known(int a);\n"
    "struct Pair { int a; int b; };\n#endif\n"
)


def snap(tmp_path: Path, files: dict[str, str]):
    root = tmp_path / "proj"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    group = SimpleNamespace(segment="main", name="grp")
    return SimpleNamespace(
        config=SimpleNamespace(project=SimpleNamespace(root=root, toolchain="t")),
        overlays={},
        layout=SimpleNamespace(groups={"g": group}),
        read=lambda p: (root / p).read_bytes(),
        peek=lambda p: (root / p).read_bytes() if (root / p).exists() else None,
    )


def unit():
    return SimpleNamespace(path="src/a.c", group="g")


def view(text):
    return SimpleNamespace(text=text, version="a")


def _known(monkeypatch, evidence):
    row = {"evidence": evidence, "signature": "int fresh(int b);"}
    monkeypatch.setattr(types, "load", lambda s: {"function": {"fresh": row}})


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    """The catalog fans out to the pool and its cache; these tests run both in process."""
    monkeypatch.setattr(headers.pool, "map",
                        lambda config, name, function, items, key=None: [function(i) for i in items])
    monkeypatch.setattr(headers.store, "cached", lambda config, kind, key, produce: produce())


@pytest.fixture
def no_types(monkeypatch):
    monkeypatch.setattr(types, "load", lambda s: {"function": {}})


def test_catalog_names(tmp_path):
    result = headers.catalog(snap(tmp_path, {"include/g.h": HEADER}), "a")
    assert {"s32", "gCount", "known", "struct Pair"} <= set(result) and "Pair" not in result
    assert result["gCount"][:2] == ("include/g.h", 4)


@pytest.mark.parametrize("files, agreed, conflicts", [
    # one type in a header and two sources: it wins, whichever file spells it
    ({"include/g.h": "extern float D_1;\n", "src/a.c": "extern  float  D_1;\nint f(void) { return 0; }\n",
      "src/b.c": "extern float D_1;\n"}, {"D_1": "float @"}, {}),
    # a pointer, an array and a prototype keep what follows and precedes the name
    ({"src/a.c": "extern char *D_2[];\nextern int p(unsigned short);\n", "src/b.c": "extern char *D_2[];\n"},
     {"D_2": "char * @[]", "p": "int @(unsigned short)"}, {}),
    # several declarators in one statement
    ({"src/a.c": "extern const f32 D_3[], D_4[];\n", "src/b.c": "extern const f32 D_4[];\n"},
     {"D_3": "const f32 @[]", "D_4": "const f32 @[]"}, {}),
    # two types: refused with every declaration, file and line
    ({"src/a.c": "extern u32 D_5;\n", "src/b.c": "\nextern s32 D_5;\n"}, {},
     {"D_5": ["D_5: src/a.c:1 extern u32 D_5;", "D_5: src/b.c:2 extern s32 D_5;"]}),
    # a function body is not read for declarations, and f32 is float
    ({"src/a.c": "int f(void) { if (1) { return 1; } return 0; }\nextern f32 D_8;\nextern float D_8;\n"},
     {"D_8": "f32 @"}, {}),
    # a name some source defines has its type from that definition
    ({"src/a.c": "extern u32 D_6;\n", "src/b.c": "const float D_6 = 1.0f;\n"}, {}, {}),
])
def test_disagreements_give_one_type_per_name_where_the_declarations_agree(tmp_path, files, agreed, conflicts):
    got, findings = headers.disagreements(snap(tmp_path, files))
    assert got == agreed
    assert {f.unit: list(f.missing) for f in findings} == conflicts
    assert all(not f.blocking and f.key == "types.conflict" for f in findings)


def test_disagreements_leave_out_the_names_the_caller_defines(tmp_path):
    files = {"src/a.c": "extern u32 D_7;\n", "src/b.c": "extern s32 D_7;\n"}
    assert headers.disagreements(snap(tmp_path, files), {"D_7"}) == ({}, [])


def test_fold_moves_new_prototype_and_removes_known(tmp_path, no_types):
    source = "void known(int a);\nvoid fresh(int b);\nint f(void) { return 0; }\n"
    s = snap(tmp_path, {"include/g.h": HEADER, "src/a.c": source})
    text = '# 1 "src/a.c"\nvoid known(int a);\nvoid fresh(int b);\nint f(void) { return 0; }\n'
    writes, findings = headers.fold(s, unit(), view(text))
    assert findings == ()
    assert "void fresh(int b);" in writes["include/main/grp.h"].decode()
    assert "fresh" not in writes["src/a.c"].decode()
    assert "known(int a);" not in writes["src/a.c"].decode()
    assert '#include "g.h"' in writes["src/a.c"].decode()


def test_fold_moves_a_typedef_whose_name_is_also_a_struct_tag(tmp_path, no_types):
    header = "struct Obj { int a; };\n"
    source = "typedef struct { int b; } Obj;\nint f(Obj *o) { return o->b; }\n"
    s = snap(tmp_path, {"include/g.h": header, "src/a.c": source})
    text = '# 1 "src/a.c"\n' + source
    writes, findings = headers.fold(s, unit(), view(text))
    assert "typedef struct { int b; } Obj;" in writes["include/main/grp.h"].decode()  # moved, not dropped as known
    assert "include/g.h" not in writes and not findings


@pytest.mark.parametrize("candidate, conflicts", [
    ("extern s32 gCount;", False), ("extern int gCount;", False), ("extern s32 gCount[];", True),
    ("extern float gCount;", True),
])
def test_fold_refuses_a_declaration_of_a_known_name_with_another_type(tmp_path, no_types, candidate, conflicts):
    s = snap(tmp_path, {"include/g.h": HEADER, "src/a.c": candidate + "\n"})
    text = '# 1 "include/g.h"\ntypedef int s32;\n# 1 "src/a.c"\n' + candidate + "\n"
    _, findings = headers.fold(s, unit(), view(text))
    assert [f.key for f in findings] == (["headers.conflict"] if conflicts else [])
    if conflicts:
        assert "gCount" in findings[0].reason and "include/g.h:4" in findings[0].reason


def test_fold_includes_in_the_group_header_what_its_moved_declarations_name(tmp_path, no_types):
    source = "typedef struct { s32 a; } Rec;\nint f(Rec *r) { return r->a; }\n"
    s = snap(tmp_path, {"include/g.h": HEADER, "src/a.c": source})
    writes, findings = headers.fold(s, unit(), view('# 1 "include/g.h"\ntypedef int s32;\n# 1 "src/a.c"\n' + source))
    header = writes["include/main/grp.h"].decode()
    assert not findings
    assert "typedef struct { s32 a; } Rec;" in header
    assert header.index('#include "g.h"') < header.index("typedef struct")
    assert '#include "main/grp.h"' in writes["src/a.c"].decode()


@pytest.mark.parametrize("evidence", ["authored", "landed"])
def test_fold_conflict_with_landed_signature(tmp_path, monkeypatch, evidence):
    _known(monkeypatch, evidence)
    s = snap(tmp_path, {"include/g.h": HEADER, "src/a.c": "void fresh(int b);\n"})
    writes, findings = headers.fold(s, unit(), view('# 1 "src/a.c"\nvoid fresh(int b);\n'))
    assert [f.key for f in findings] == ["headers.conflict"]
    assert "candidate declares" in findings[0].reason and "type map has int fresh(int b);" in findings[0].reason
    assert findings[0].path == "src/a.c"
    assert "include/main/grp.h" not in writes


def test_fold_no_conflict_when_inferred_or_same(tmp_path, monkeypatch):
    _known(monkeypatch, "inferred")
    s = snap(tmp_path, {"include/g.h": HEADER, "src/a.c": "void fresh(int b);\n"})
    _, findings = headers.fold(s, unit(), view('# 1 "src/a.c"\nvoid fresh(int b);\n'))
    assert findings == ()


def _stub_context(monkeypatch, extra):
    monkeypatch.setattr(types, "context", lambda s: extra)
    monkeypatch.setattr(layout, "overlay", lambda s, w: s)
    monkeypatch.setattr(recipes, "resolve", lambda c, u, o: None)
    monkeypatch.setattr(view_module, "get", lambda *a: SimpleNamespace(text="int cat;\n", key="view"))


def test_context_includes_type_map(tmp_path, monkeypatch):
    _stub_context(monkeypatch, "typedef int mapped;\n")
    out = headers.context(snap(tmp_path, {"include/g.h": HEADER}), "a")
    text = out.read_text()
    assert text.index("int cat;") < text.index("typedef int mapped;")
    assert out.suffix == ".i"


def test_context_key_changes_with_types(tmp_path, monkeypatch):
    s = snap(tmp_path, {"include/g.h": HEADER})
    _stub_context(monkeypatch, "one\n")
    first = headers.context(s, "a")
    _stub_context(monkeypatch, "two\n")
    second = headers.context(s, "a")
    assert first != second


def test_context_refusal_is_draft_context(tmp_path, monkeypatch):
    _stub_context(monkeypatch, "x\n")
    def boom(*a):
        raise OSError("no")
    monkeypatch.setattr(view_module, "get", boom)
    with pytest.raises(Refusal) as error:
        headers.context(snap(tmp_path, {"include/g.h": HEADER}), "a")
    assert error.value.findings[0].key == "draft.context"


@pytest.mark.parametrize("definition, conflicts", [
    ("int f(void) { return 0; }", False), ("s32 f(void) { return 0; }", False),
    ("void f(void) { }", True), ("int *f(void) { return 0; }", True),
])
def test_fold_refuses_a_definition_whose_header_prototype_returns_another_type(
        tmp_path, no_types, monkeypatch, definition, conflicts):
    monkeypatch.setattr(headers, "catalog", lambda snap, version: {"f": ("include/g.h", 7, "extern int f(void);")})
    s = snap(tmp_path, {"include/g.h": HEADER, "src/a.c": definition + "\n"})
    text = '# 1 "include/g.h"\ntypedef int s32;\n# 1 "src/a.c"\n' + definition + "\n"
    _, findings = headers.fold(s, unit(), view(text))
    assert [f.key for f in findings] == (["headers.conflict"] if conflicts else [])
    if conflicts:
        assert "include/g.h:7" in findings[0].reason and "declares 'int'" in findings[0].reason


def test_context_keeps_only_type_map_lines_the_headers_can_read():
    text = "typedef int s32;\nstruct Query { s32 a; };\n"
    extra = "void a(s32);\nvoid b(void *, Query *);\nextern u8 resources/rsp/x.s[8];\nvoid c(struct Query *);"
    kept, dropped = headers._parseable(text, extra)
    assert kept == "void a(s32);\nvoid c(struct Query *);\n"
    assert dropped == ["void b(void *, Query *);", "extern u8 resources/rsp/x.s[8];"]
