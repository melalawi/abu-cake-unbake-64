"""View contract tests with an in-memory cache and canned preprocessor output."""

import json
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fixture
import pytest

from unbake import view
from unbake.contracts import (
    Config,
    Finding,
    Host,
    LayoutMap,
    Project,
    Recipe,
    Refusal,
    Snapshot,
    UnitSpec,
    digest,
)


def _sha(data):
    return sha256(data).hexdigest()


@pytest.fixture
def lane(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "include").mkdir()
    (root / "src/unit.c").write_bytes(b'#include "active.h"\nint value;\n')
    (root / "include/active.h").write_bytes(b"typedef int active;\n")
    (root / "include/z.h").write_bytes(b"typedef int z;\n")
    versions = ("a", "b", "c", "d", "e")
    project = Project(
        root=root, id="fixture", name="fixture", title="Fixture", versions=versions,
        names_from="a", toolchain="fake", build={}, version_files={},
        version_macros={v: (f"VERSION_{v.upper()}",) for v in versions},
        resident={}, layout_cap=200, origins={}, digest=digest("project"),
    )
    host = Host(
        cores=2, workers=2, memory_parent_bytes=1024, memory_worker_bytes=1024,
        cache_max_bytes=1024, toolchain_root=tmp_path / "tools",
        tools={}, sdk_catalog=None, serial_seconds=2, serial_cores=2, pool_fill=0.8, pool_fanout=2,
        author=("test", "test@example.invalid"), origins={}, digest=digest("host"),
        budget_dir=tmp_path / "budget")
    config = Config(project, host, digest("config"))
    unit = UnitSpec("src/unit.c", "c", "group", ("value",), "fake", {})
    layout = LayoutMap(200, {}, {}, {unit.path: unit}, digest("layout"), (), {})
    snapshot = Snapshot(config, "0" * 40, layout, {}, {}, digest("snapshot"))
    recipe = Recipe("fake", (), (), (), digest("recipe"))
    state = SimpleNamespace(
        root=root, snapshot=snapshot, unit=unit, recipe=recipe, cache={}, events=[],
        text=f'# 1 "{root / unit.path}"\nint value;\n', result=fixture.native_result(), findings=(),
    )

    def preprocess(source, recipe, version, include, *, out):
        out.write_text(state.text)
        return state.result

    state.preprocess = Mock(side_effect=preprocess)
    state.diagnose = Mock(side_effect=lambda result: state.findings)
    state.toolchain = Mock(return_value=SimpleNamespace(preprocess=state.preprocess, diagnose=state.diagnose))

    def cached(config, kind, key, produce):
        hit = (kind, key) in state.cache
        state.events.append((kind, hit))
        if not hit:
            state.cache[kind, key] = produce()
        return state.cache[kind, key]

    def put(config, kind, key, value):
        state.cache[kind, key] = value

    state.cached = Mock(side_effect=cached)
    state.put = Mock(side_effect=put)
    state.pool = Mock(side_effect=lambda config, name, function, items, key=None: [function(item) for item in items])
    state.stage = Mock(side_effect=lambda name: nullcontext())
    monkeypatch.setattr(view.adapters, "toolchain", state.toolchain)
    monkeypatch.setattr(view.store, "cached", state.cached)
    monkeypatch.setattr(view.store, "put", state.put)
    monkeypatch.setattr(view.pool, "map", state.pool)
    monkeypatch.setattr(view.effort, "stage", state.stage)
    return state


def _get(lane, snapshot=None, recipe=None):
    return view.get(snapshot or lane.snapshot, lane.unit, "a", recipe or lane.recipe)


def _overlay(snapshot, writes):
    return replace(snapshot, overlays=writes, digest=digest((snapshot.digest, writes)))


def test_cardinality_equals_request(lane):
    requested = ("e", "c", "a", "d", "b")
    result = view.all_versions(lane.snapshot, lane.unit, lane.recipe, requested)
    assert list(result) == list(requested)
    assert len(result) == 5
    assert len({item.text for item in result.values()}) == 1
    assert [item.version for item in result.values()] == list(requested)
    assert lane.preprocess.call_count == 5
    config, name, function, items = lane.pool.call_args.args
    assert config is lane.snapshot.config
    assert name == "view.all_versions"
    assert function.__module__ == "unbake.view"
    assert function.__qualname__ == "_one"
    assert items == [(lane.snapshot, lane.unit, version, lane.recipe) for version in requested]
    assert lane.stage.call_args_list[0].args == ("view.all_versions",)


def test_all_versions_empty(lane):
    assert view.all_versions(lane.snapshot, lane.unit, lane.recipe, ()) == {}
    lane.preprocess.assert_not_called()


def test_cache_hit_second_call(lane):
    first = _get(lane)
    second = _get(lane)
    assert first == second
    lane.preprocess.assert_called_once()
    assert lane.events == [("view", False), ("view", True)]
    lane.toolchain.assert_called_once_with(lane.snapshot.config, lane.recipe.toolchain)
    assert lane.stage.call_count == 2
    assert all(call.args == ("view.get",) for call in lane.stage.call_args_list)
    args, kwargs = lane.preprocess.call_args
    assert args == (lane.root / lane.unit.path, lane.recipe, "a", [lane.root / "include", lane.root / "src"])
    assert kwargs["out"].name == "view.i"


@pytest.mark.parametrize("overlay", [False, True], ids=["disk-header", "overlay-header"])
def test_header_change_invalidates(lane, overlay):
    header = "include/active.h"
    lane.text = f'# 1 "{header}"\ntypedef int active;\n# 1 "src/unit.c"\nint value;\n'
    first = _get(lane)
    changed = b"typedef long active;\n"
    snapshot = lane.snapshot
    if overlay:
        snapshot = _overlay(snapshot, {header: changed})
    else:
        (lane.root / header).write_bytes(changed)
        view.effort._memo.clear()  # the next command reads the file again
    lane.text = lane.text.replace("typedef int active;", "typedef long active;")
    second = _get(lane, snapshot)
    assert lane.preprocess.call_count == 2
    assert second.key != first.key
    assert dict(second.dependencies)[header] == _sha(changed)
    assert _get(lane, snapshot) == second
    assert lane.preprocess.call_count == 2
    assert json.loads(lane.cache["view", second.key])["deps"] == [list(row) for row in second.dependencies]


@pytest.mark.parametrize("source_overlaid", [False, True])
def test_overlay_paths_mapped_back(lane, source_overlaid):
    writes = {"include/active.h": b"typedef long active;\n", "include/deleted.h": None}
    if source_overlaid:
        writes[lane.unit.path] = b"int changed;\n"
    snapshot = _overlay(lane.snapshot, writes)
    overlay = lane.root / "build/views" / snapshot.digest[:16]
    source = (overlay if source_overlaid else lane.root) / lane.unit.path
    outside = lane.root.parent / "system.h"
    lane.text = (
        f'# 7 "{overlay / "include/active.h"}" 1\ntypedef long active;\n'
        f'# 3 "{outside}"\nint external;\n# 0 "<built-in>"\nint builtin;\n'
        f'# 9 "{source}" 2\nint changed;\n'
    )
    result = _get(lane, snapshot)
    assert result.lines == (
        (2, "include/active.h", 7), (4, str(outside), 3),
        (6, "<built-in>", 0), (8, lane.unit.path, 9),
    )
    assert result.dependencies == (
        (lane.unit.path, _sha(snapshot.read(lane.unit.path))),
        ("include/active.h", _sha(writes["include/active.h"])),
    )
    assert (overlay / "include/active.h").read_bytes() == writes["include/active.h"]
    assert not (overlay / "include/deleted.h").exists()
    assert lane.preprocess.call_args.args == (
        source, lane.recipe, "a",
        [overlay / "include", lane.root / "include", overlay / "src", lane.root / "src"],
    )
    # A different recipe causes another preprocessing pass but reuses materialisation.
    (overlay / "include/active.h").write_bytes(b"sentinel")
    _get(lane, snapshot, replace(lane.recipe, digest=digest("other-recipe")))
    assert (overlay / "include/active.h").read_bytes() == b"sentinel"


def test_an_overlaid_file_finds_the_files_it_names_relative_to_itself(lane):
    (lane.root / "include/sub").mkdir()
    (lane.root / "include/base.h").write_bytes(b'#include "sub/deep.h"\ntypedef int base;\n')
    (lane.root / "include/sub/deep.h").write_bytes(b"typedef int deep;\n")
    snapshot = _overlay(lane.snapshot, {"include/sub/new.h": b'#include "../base.h"\n'})
    overlay = lane.root / "build/views" / snapshot.digest[:16]
    _get(lane, snapshot)
    assert (overlay / "include/base.h").read_bytes() == (lane.root / "include/base.h").read_bytes()
    assert (overlay / "include/sub/deep.h").read_bytes() == b"typedef int deep;\n"  # and what that file names
    assert not (overlay / "include/z.h").exists()  # nothing else is copied


def test_inactive_include_not_a_dependency(lane):
    source = b'#if 0\n#include "inactive.h"\n#endif\nint value;\n'
    (lane.root / lane.unit.path).write_bytes(source)
    lane.text = '# 1 "src/unit.c"\nint value;\n'
    first = _get(lane)
    assert first.dependencies == ((lane.unit.path, _sha(source)),)
    (lane.root / "include/inactive.h").write_bytes(b"changed but inactive")
    assert _get(lane) == first
    lane.preprocess.assert_called_once()


@pytest.mark.parametrize(
    ("path", "expected"),
    [("src/unit.c", {10: "int value;", 11: "", 20: "int other;"}),
     ("include/active.h", {4: "typedef int active;"}), ("unknown.h", {})],
)
def test_active_lines_by_file(lane, path, expected):
    lane.text = (
        '# 4 "include/active.h"\ntypedef int active;\n'
        '# 10 "src/unit.c"\nint value;\n\n# 20 "src/unit.c"\nint other;\n'
    )
    result = _get(lane)
    assert view.active_lines(result, path) == expected
    assert lane.stage.call_args.args == ("view.active_lines",)


def test_dependency_order_and_cache_keys(lane):
    lane.text = (
        '# 1 "include/z.h"\nint z;\n# 2 "include/active.h"\nint active;\n'
        '# 1 "src/unit.c"\nint value;\n# 5 "include/z.h"\nint z_again;\n'
    )
    result = _get(lane)
    deps = [(path, _sha(lane.snapshot.read(path))) for path in
            (lane.unit.path, "include/active.h", "include/z.h")]
    reads = view.closure(lane.snapshot, lane.unit, "a")  # what the build can reach, pinned: the one key
    key = digest((lane.unit.path, _sha(lane.snapshot.read(lane.unit.path)), lane.recipe.digest, "a",
                  lane.snapshot.config.project.version_macros["a"], view._CODE, reads))
    assert result.dependencies == tuple(deps)
    assert result.key == key
    assert json.loads(lane.cache["view", key]) == {"text": lane.text, "deps": [list(row) for row in deps]}


@pytest.mark.parametrize("stderr", [
    b"fatal error: absent.h: No such file or directory\n",
    b'Cannot open include file "absent.h"\n',
])
def test_missing_include_refuses(lane, stderr):
    finding = Finding("preprocess.error", stderr.decode())
    lane.result = fixture.native_result(exit=1, stderr=stderr)
    lane.findings = (finding,)
    with pytest.raises(Refusal) as raised:
        _get(lane)
    assert raised.value.findings[0] == finding
    assert raised.value.findings[1].key == "view.include"
    assert raised.value.findings[1].missing == ("absent.h",)
    assert not lane.cache


def test_diagnose_findings_refuse_even_on_zero_exit(lane):
    lane.findings = (Finding("preprocess.error", "Rejected preprocessing."),)
    with pytest.raises(Refusal) as raised:
        _get(lane)
    assert raised.value.findings == lane.findings
    lane.diagnose.assert_called_once_with(lane.result)


@pytest.mark.parametrize("deleted", [False, True])
def test_missing_source_refuses(lane, deleted):
    snapshot = lane.snapshot
    if deleted:
        snapshot = _overlay(snapshot, {lane.unit.path: None})
    else:
        (lane.root / lane.unit.path).unlink()
    with pytest.raises(Refusal) as raised:
        _get(lane, snapshot)
    assert raised.value.findings[0].key == "preprocess.error"
    assert raised.value.findings[0].path == lane.unit.path
    lane.preprocess.assert_not_called()


def test_header_change_invalidates_when_the_preprocessor_prints_no_line_markers(lane):
    lane.text = "int value;\n"  # a -P preprocessor: no markers say which headers were read
    first = _get(lane)
    assert [path for path, _ in first.dependencies] == ["src/unit.c", "include/active.h"]
    (lane.root / "include/active.h").write_bytes(b"typedef long active;\n")
    view.effort._memo.clear()  # the next command reads the file again
    assert _get(lane).key != first.key
    assert lane.preprocess.call_count == 2


def test_the_view_is_preprocessed_with_line_markers_even_when_the_recipe_asks_for_p(lane):
    recipe = Recipe("fake", ("-P", "-undef"), (), (), digest("recipe-p"))
    _get(lane, recipe=recipe)
    assert lane.preprocess.call_args.args[1].cppflags == ("-undef",)
    assert lane.preprocess.call_args.args[1].digest == recipe.digest


def test_a_relative_include_beside_an_overlaid_header_is_linked_from_the_real_tree(lane):
    (lane.root / "include/sub").mkdir()
    (lane.root / "include/types.h").write_bytes(b'#include "z.h"\ntypedef int t;\n')
    writes = {"include/sub/a.h": b'#include "../types.h"\n'}
    snapshot = _overlay(lane.snapshot, writes)
    _get(lane, snapshot)
    overlay = lane.root / "build/views" / snapshot.digest[:16]
    assert (overlay / "include/types.h").resolve() == (lane.root / "include/types.h").resolve()
    assert (overlay / "include/z.h").is_symlink()


def test_two_creators_of_the_same_overlay_link_both_succeed(tmp_path, monkeypatch):
    root, overlay = tmp_path / "root", tmp_path / "overlay"
    (root / "include").mkdir(parents=True)
    (root / "include/a.h").write_text("int a;\n")
    (overlay / "include").mkdir(parents=True)
    (overlay / "include/a.h").symlink_to(root / "include/a.h")  # the other job's link, made after our check
    with monkeypatch.context() as stale:  # our check ran before it existed
        stale.setattr(Path, "exists", lambda self: False)
        stale.setattr(Path, "is_symlink", lambda self: False)
        view._link_relative(overlay, root, "include/b.h", b'#include "a.h"\n')
    link = overlay / "include/a.h"
    assert link.is_symlink() and link.resolve() == (root / "include/a.h").resolve()
    assert [p.name for p in (overlay / "include").iterdir()] == ["a.h"]
