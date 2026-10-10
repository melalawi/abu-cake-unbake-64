"""Portable build rendering and mocked native checks; no ROMs or builds."""

import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fixture
import pytest

from unbake import build, native, process, recipes, store
from unbake import config as configuration
from unbake.contracts import (
    Config,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Recipe,
    Refusal,
    Snapshot,
    UnitSpec,
    Version,
    VersionFiles,
    digest,
)


@pytest.fixture(autouse=True)
def isolated_toolchains(toolchains):
    # conftest reuses TOOLCHAIN_ROW; keep all mutations local to this test.
    toolchains["toolchain"] = deepcopy(toolchains["toolchain"])


@pytest.fixture
def build_snapshot(tmp_path, toolchains, monkeypatch):
    tools = {name: tmp_path / "host-bin" / name for name in fixture.TOOLS}
    host = Host(2, 3, 1024, 1024, 1024, tmp_path / "chains",
                tools, None, 2, 2, 0.8, 2, ("test", "test@invalid"), {}, "host")
    files = {v: VersionFiles(f"roms/{v}.z64", str(i) * 40, f"versions/{v}/Game.yaml",
                             f"versions/{v}/symbols.txt", {}) for i, v in enumerate(("a", "b"), 1)}
    project = Project(tmp_path, "fixture", "fixture", "Fixture title", ("b", "a"), "a", "gcc-test",
                      {}, files, {v: (f"-DVERSION_{v.upper()}",) for v in files},
                      {v: () for v in files}, 200, {}, "project")
    cfg = Config(project, host, "config")
    placements = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in files)
    member = Member("function", "function", "c", "group", placements)
    unit = UnitSpec("src/group.c", "c", "group", (member.name,), "gcc-test", {})
    layout = LayoutMap(200, {}, {member.name: member}, {unit.path: unit}, "layout", (), {})
    versions = {v: Version(v, tmp_path / files[v].baserom, "sha", files[v].split, files[v].symbols,
                           {"z": 0x80000408, "a": 0x80000400}, ()) for v in files}
    snapshot = Snapshot(cfg, "head", layout, versions, {}, "snapshot")
    monkeypatch.setattr(recipes, "resolve", lambda cfg, unit, overrides:
                        Recipe(unit.toolchain, ("-E",), ("-O2",), ("-EB",), digest((unit.toolchain, unit.options))))
    monkeypatch.setattr(native, "sections", lambda snap, unit, version:
                        tuple(p for n in unit.members for p in snap.layout.members[n].placements
                              if p.version == version))
    return snapshot


@pytest.fixture(autouse=True)
def native_mock(monkeypatch):
    run = Mock(return_value=fixture.native_result())
    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(process, "tool", lambda cfg, name: cfg.host.tools[name])
    return run


@pytest.fixture(autouse=True)
def fragment_cache(monkeypatch):
    values, calls = {}, []

    def cached(cfg, kind, key, produce):
        hit = (kind, key) in values
        calls.append((kind, key, hit))
        if not hit:
            values[kind, key] = produce()
        return values[kind, key]

    monkeypatch.setattr(store, "cached", cached)
    return calls


def _unit(snapshot):
    return next(iter(snapshot.layout.units.values()))


def _with_unit(snapshot, unit):
    return replace(snapshot, layout=replace(snapshot.layout, units={unit.path: unit}))


def _download():
    return {"url": "https://example.invalid/toolchain.tar.gz", "sha256": "a" * 64, "files": ["cc", "as1"]}


def test_makefile_has_no_host_paths(build_snapshot, native_mock):
    text = build.makefile(build_snapshot).decode()
    host = build_snapshot.config.host
    assert str(host.toolchain_root) not in text
    assert str(build_snapshot.config.project.root) not in text
    assert all(str(path) not in text for path in host.tools.values())
    assert "NAME := fixture" in text
    assert "VERSIONS := b a" in text
    assert "Fixture title" in text
    native_mock.assert_not_called()


@pytest.mark.parametrize("kind", ["asm", "c"])
def test_asm_unit_uses_gnu_assembler_var(build_snapshot, kind):
    unit = replace(_unit(build_snapshot), path="src/group.s" if kind == "asm" else "src/group.c",
                   kind=kind, toolchain="ido-7.1")
    snapshot = _with_unit(build_snapshot, unit)
    snapshot = replace(snapshot, config=replace(snapshot.config,
                                               project=replace(snapshot.config.project, toolchain="ido-7.1")))
    text = build.makefile(snapshot).decode()
    if kind == "asm":
        assert "'$(AS)' -EB -Iinclude -Isrc -Iasm/a/include src/group.s -o" in text
        assert "'$(AS_ido_7_1)'" not in text
        assert "$(TOOLCHAIN_ido_7_1)" not in text
    else:
        assert "'$(CC_ido_7_1)'" in text
        assert "$(TOOLCHAIN_ido_7_1)" in text
        assert "'$(AS)' -EB" not in text


def test_makefile_tree_and_rom_checks(build_snapshot):
    text = build.makefile(build_snapshot).decode()
    assert "ifeq ($(CHECK),tree)" in text
    assert "else ifeq ($(CHECK),rom)" in text
    assert "check: build/b/fixture.z64 build/a/fixture.z64" in text
    for version, files in build_snapshot.config.project.version_files.items():
        rom = f"build/{version}/fixture.z64"
        assert f"\t$(Q)test -s versions/{version}/report.json" in text
        assert f'\t$(Q)test "$$(sha1sum < {rom} | cut -d\' \' -f1)" = {files.baserom_sha1}' in text
        assert f"{rom}: {files.baserom} Makefile" in text
        assert f"cp {files.baserom} $@" in text
    assert "for p in $(PATCHES); do dd if=$${p%:*} of=$@ bs=1 seek=$${p##*:}" in text
    assert "conv=notrunc status=none || exit 1" in text


def test_toolchain_rule_pins(build_snapshot, toolchains):
    row = toolchains["toolchain"]["gcc-test"]
    download = _download()
    row.update(downloads=[download], pins={"z-file": "b" * 64, "a-file": "c" * 64})
    text = build.makefile(build_snapshot).decode()
    assert "CC_gcc_test ?= tools/gcc-test/cc" in text
    assert "AS_gcc_test ?= $(AS)" in text
    assert "TOOLCHAIN_gcc_test ?= tools/gcc-test/.stamp" in text
    assert "tools/gcc-test/.stamp:" in text
    assert f"curl -fsSL {download['url']} -o tools/gcc-test.tar.gz" in text
    assert f"echo '{download['sha256']}  tools/gcc-test.tar.gz'" in text
    assert "tar -xzf tools/gcc-test.tar.gz -C tools/gcc-test cc as1" in text
    assert "'" + "c" * 64 + "  a-file' '" + "b" * 64 + "  z-file'" in text
    assert "cd tools/gcc-test" in text
    assert "touch .stamp" in text


def test_no_download_requires_local_cc(build_snapshot):
    text = build.makefile(build_snapshot).decode()
    assert "CC_gcc_test ?= $(error CC_gcc_test must be set in local.mk)" in text
    assert "TOOLCHAIN_gcc_test ?=\n" in text
    assert "curl -fsSL" not in text


@pytest.mark.parametrize("id", ["gcc-test", "ido-7.1"])
def test_two_downloads_refuse(build_snapshot, toolchains, id):
    toolchains["toolchain"][id]["downloads"] = [_download(), _download()]
    snapshot = _with_unit(build_snapshot, replace(_unit(build_snapshot), toolchain=id))
    with pytest.raises(Refusal) as caught:
        build.makefile(snapshot)
    finding = caught.value.findings[0]
    assert finding.key == "toolchain.download"
    assert finding.reason == f"{id} has 2 downloads; the generated build supports one"
    assert finding.path == id


def test_local_mk_host_paths(build_snapshot, toolchains):
    cfg = build_snapshot.config
    text = build.local_mk(cfg).decode()
    tools = configuration.load_resource("repo.toml")["make"]["tools"]
    for var, row in tools.items():
        assert f"{var} := {cfg.host.tools[row['host']]}" in text
    for id, row in toolchains["toolchain"].items():
        key = id.replace("-", "_").replace(".", "_")
        assert f"CC_{key} := {cfg.host.toolchain_root / id / row['cc']}" in text
        expected = str(cfg.host.toolchain_root / id / row["as"]) if "as" in row else "$(AS)"
        assert f"AS_{key} := {expected}" in text
        assert f"TOOLCHAIN_{key} :=\n" in text


@pytest.mark.parametrize("exit", [1, 2, None])
def test_make_check_refuses_on_exit(build_snapshot, native_mock, exit):
    lines = [f"diagnostic {i}" for i in range(25)]
    native_mock.return_value = fixture.native_result(exit=exit, stderr="\n".join(lines).encode())
    with pytest.raises(Refusal) as caught:
        build.make_check(build_snapshot.config)
    finding = caught.value.findings[0]
    assert finding.key == "setup.make"
    assert finding.reason == "\n".join(lines[-20:])
    assert finding.missing == (f"make exit {exit}",)


def test_make_check_success(build_snapshot, native_mock):
    cfg = build_snapshot.config
    assert build.make_check(cfg) == {"exit": 0, "versions": ["b", "a"]}
    make = str(cfg.host.tools["make"])
    native_mock.assert_called_once_with("make check", [make, "-k", "-j3", "check"], cfg.project.root,
                                       tmp=cfg.project.root / ".unbake" / "tmp")


def test_make_check_is_skipped_while_its_inputs_are_unchanged(build_snapshot, native_mock):
    cfg = build_snapshot.config
    build.make_check(cfg)
    build.make_check(cfg)
    native_mock.assert_called_once()  # the second check found the same inputs
    (cfg.project.root / "Makefile").write_text("changed\n")
    build.make_check(cfg)
    assert native_mock.call_count == 2


def test_rule_outputs_live_in_a_directory_named_by_the_digest_of_the_rule(build_snapshot, monkeypatch, toolchains):
    """A rule has no Makefile dependency: a changed rule has other outputs, an unchanged one keeps its own."""
    def tags(snapshot):
        return re.findall(r"build/[ab]/src/group/(\w{12})/text\.bin", build.makefile(snapshot).decode())
    first = tags(build_snapshot)
    assert len(set(first)) == 2 and tags(build_snapshot) == first  # one per version, the same on every run
    toolchains["toolchain"]["gcc-test"]["compile"].append("-changed")
    assert not set(tags(build_snapshot)) & set(first)


def test_a_rule_does_not_depend_on_the_host_or_the_project_root(build_snapshot):
    other_host = replace(build_snapshot.config.host, workers=7, digest="another host")
    moved = replace(build_snapshot, config=replace(build_snapshot.config, host=other_host, digest="another config"))
    assert build.makefile(moved) == build.makefile(build_snapshot)


def test_unit_fragment_placeholders_and_dependencies(build_snapshot):
    text = build.makefile(build_snapshot).decode()
    assert text.index("# src/group.c (a)") < text.index("# src/group.c (b)")
    for v in ("a", "b"):
        assert f"# src/group.c ({v})" in text
        assert re.search(rf"build/{v}/src/group/\w{{12}}/text\.bin &: src/group.c versions/{v}/symbols.ld", text)
        rule = re.search(rf"build/{v}/src/group/\w{{12}}/text\.bin &:[^\n]*", text)[0]
        assert rule.endswith(f"versions/{v}/symbols.ld $(wildcard include/*.h include/*/*.h) $(TOOLCHAIN_gcc_test)")
        assert f"-Iinclude -Isrc -Iasm/{v}/include" in text
        assert re.search(rf"build/{v}/fixture.z64: PATCHES \+= build/{v}/src/group/\w{{12}}/text.bin:4096", text)
    assert "'$(LD)'" in text and "n64link place" not in text.lower() and " place " not in text and "SUBALIGN(4)" in text
    assert "set-section-flags" not in text and "normal.o" not in text and "placed.o" not in text
    assert all("--rom" not in line and "--map" not in line for line in text.splitlines())


def test_symbols_ld_sorted(build_snapshot):
    assert build.symbols_ld(build_snapshot, "a") == b"PROVIDE(a = 0x80000400);\nPROVIDE(z = 0x80000408);\n"


@pytest.mark.parametrize("exit", [0, 1])
def test_extract_mocked(build_snapshot, native_mock, exit):
    split = build_snapshot.config.project.root / build_snapshot.config.project.version_files["a"].split
    split.parent.mkdir(parents=True, exist_ok=True)
    split.write_text("options: {basename: rom}\nsegments: []\n")
    cfg = build_snapshot.config
    native_mock.return_value = fixture.native_result(exit=exit, stderr=b"splat failed")
    if exit:
        with pytest.raises(Refusal) as caught:
            build.extract(cfg, "a")
        assert caught.value.findings[0].key == "setup.splat"
        assert caught.value.findings[0].versions == ("a",)
    else:
        assert build.extract(cfg, "a") is native_mock.return_value
    argv = native_mock.call_args.args[1]
    assert argv[:2] == [cfg.host.tools["splat"], "split"]
    assert argv[3:] == ["--make-full-disasm-for-code"]
    assert argv[2] != str(split)
    assert native_mock.call_args.args[2] == cfg.project.root

def test_split_input_bounds_long_output_names_and_preserves_logical_input():
    import yaml
    long_name = "rodata/" + "unclaimed_" * 40
    original = {"options": {"base_path": "../.."}, "segments": [
        {"name": "m", "type": "code", "start": 0, "subsegments": [
            [0, "data", long_name], {"start": 4, "type": "data", "name": long_name + "b"}, [8]]}, [8]]}
    source = yaml.safe_dump(original)
    document = build._split_input(source)
    rows = document["segments"][0]["subsegments"]
    assert all(len(name.split("/")[-1].encode()) <= 240 for name in [rows[0][2], rows[1]["name"]])
    assert rows[0][2] != rows[1]["name"]
    assert build._split_input(source) == document
    assert yaml.safe_load(source) == original
    assert build._split_input(yaml.safe_dump(document)) is None


def test_split_input_preserves_normal_config():
    assert build._split_input("options: {basename: rom}\nsegments:\n - [0]\n") is None

def test_extract_long_names_uses_private_config(build_snapshot, native_mock):
    import yaml
    cfg = build_snapshot.config
    split = cfg.project.root / cfg.project.version_files["a"].split
    split.parent.mkdir(parents=True, exist_ok=True)
    original = yaml.safe_dump({"options": {"base_path": "../.."}, "segments": [
        {"name": "main", "type": "code", "start": 0, "subsegments": [[0, "data", "d" * 300], [4]]}, [4]]})
    split.write_text(original)
    private = []
    def invoke(name, argv, cwd, **kw):
        path = Path(argv[2])
        private.append(path)
        document = yaml.safe_load(path.read_text())
        assert path != split and document["options"]["base_path"] == str(path.parent)
        assert len(document["segments"][0]["subsegments"][0][2]) <= 240
        return fixture.native_result()
    native_mock.side_effect = invoke
    assert build.extract(cfg, "a").exit == 0
    assert split.read_text() == original and not private[0].exists()


def test_extract_isolates_dump_and_preserves_path_destinations(build_snapshot, native_mock):
    import yaml
    cfg = build_snapshot.config
    root = cfg.project.root
    split = root / cfg.project.version_files["a"].split
    split.parent.mkdir(parents=True, exist_ok=True)
    split.write_text(yaml.safe_dump({"options": {"base_path": "../..", "asm_path": "asm/a",
        "symbol_addrs_path": ["symbols/a.txt"], "target_path": "roms/a.z64"}, "segments": []}))
    seen = []
    def invoke(name, argv, cwd, **kw):
        path = Path(argv[2])
        options = yaml.safe_load(path.read_text())["options"]
        assert options["base_path"] == str(path.parent)
        assert options["asm_path"] == str(path.parent / "tree" / "asm/a")  # written aside, published when it differs
        assert options["symbol_addrs_path"] == [str(root / "symbols/a.txt")]
        assert options["target_path"] == str(root / "roms/a.z64")
        assert options["dump_symbols"] is True
        dump = path.parent / ".splat"
        dump.mkdir()
        (dump / "spim_context_unksegment.csv").write_text("category,address,getName\nsymbol,0x80000800,target\n")
        seen.append(path.parent)
        return fixture.native_result()
    native_mock.side_effect = invoke
    build.extract(cfg, "a")
    assert not seen[0].exists()
    assert (root / ".unbake/symbols/a/spim_context_unksegment.csv").read_text().endswith("target\n")
    assert not (root / ".splat").exists()


def test_symbols_ld_provides_code_at_the_masked_address_only_when_the_project_says_so(build_snapshot, monkeypatch):
    snapshot = build_snapshot
    record = replace(snapshot.versions["a"], symbols={"call": 0x802BB550, "data": 0x800D8A24, "label": 0x802BB700},
                     code=frozenset({"label"}))
    members = {"f": SimpleNamespace(placements=(Placement("a", ".text", 0, 0x100, 0x802BB500),
                                                Placement("a", ".text", 0x100, 0x200, 0x802BB600)))}
    project = replace(snapshot.config.project, build={**snapshot.config.project.build, "text_mask": 0x1FFFFFFF})
    masked = replace(snapshot, layout=SimpleNamespace(members=members), versions={"a": record}, digest="masked",
                     config=replace(snapshot.config, project=project))
    monkeypatch.setattr(build.symbols, "load", lambda reader, versions: {"call": {"kind": "function"}})
    text = build.symbols_ld(masked, "a").decode()
    assert "PROVIDE(call = 0x002BB550);" in text and "PROVIDE(label = 0x002BB700);" in text
    assert "PROVIDE(data = 0x800D8A24);" in text  # data keeps the address it has
    assert build._text_spans(masked, "a") == [(-1, 0), (0x802BB500, 0x802BB700)]  # runs that touch are one run
    unmasked = replace(masked, config=snapshot.config)
    assert "PROVIDE(call = 0x802BB550);" in build.symbols_ld(unmasked, "a").decode()


def test_make_check_reports_every_failed_target(build_snapshot, native_mock):
    lines = ["make: *** [a.o] Error 1", "noise", "make: *** [b.o] Error 2", "make: Target 'check' not remade"]
    native_mock.return_value = fixture.native_result(exit=2, stderr="\n".join(lines).encode())
    with pytest.raises(Refusal) as caught:
        build.make_check(build_snapshot.config)
    assert caught.value.findings[0].reason == "\n".join([lines[0], lines[2]])


