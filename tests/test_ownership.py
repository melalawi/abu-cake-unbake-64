"""ownership: a C unit owns the rodata and data its object emits, located in the ROM; each mismatch class has a case."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from unbake import ownership
from unbake.contracts import Claim, LayoutMap, Member, Placement, Snapshot, UnitSpec

TEXT_VRAM, TEXT_ROM = 0x80207738, 0x1000


def word(*values: int) -> bytes:
    return b"".join(v.to_bytes(4, "big") for v in values)


def index(rom: bytes, rows: list[tuple[int, int, str, str, int]]) -> tuple:
    """The tuple ownership._index builds: rom, rows by ROM start, start -> row, first word -> starts, digest."""
    first = defaultdict(list)
    for start, *_ in rows:
        first[int.from_bytes(rom[start:start + 4], "big")].append(start)
    by_vram = sorted((r[4], r[4] + r[1] - r[0], r[0], r[1]) for r in rows)
    return rom, rows, {r[0]: i for i, r in enumerate(rows)}, first, "rows", by_vram, ()


def emits(monkeypatch, **sections):
    monkeypatch.setattr(ownership, "emitted", lambda blob: sections)


# ------------------------------------------------------------------ what the object emits


class Section:
    def __init__(self, name, data=b"", kind="SHT_PROGBITS", align=1, link=0, info=0, relocs=(), symbols=()):
        self.name, self._data, self.relocs, self.symbols = name, data, relocs, symbols
        self.fields = {"sh_size": len(data), "sh_type": kind, "sh_link": link, "sh_info": info, "sh_addralign": align}

    def __getitem__(self, key):
        return self.fields[key]

    def data(self):
        return self._data

    def iter_relocations(self):
        return iter(self.relocs)

    def get_symbol(self, number):
        return self.symbols[number]


def test_emitted_concatenates_in_file_order_with_alignment_relocations_and_low_references(monkeypatch):
    lo16 = ownership.ENUM_RELOC_TYPE_MIPS["R_MIPS_LO16"]
    code = word(0x3C040000, 0x24840008, 0, 0)  # lui, addiu $a0, $a0, 8 (the low half references .rdata + 8)
    sections = [
        Section(".text", code),
        Section(".rodata", word(1, 2), align=4),
        Section(".rdata", word(0x34), align=8),
        Section(".sdata", word(7)),
        Section(".rel.text", kind="SHT_REL", link=6, info=0, relocs=[
            {"r_offset": 4, "r_info_sym": 0, "r_info_type": lo16}]),
        Section(".rel.rodata", kind="SHT_REL", link=6, info=1, relocs=[
            {"r_offset": 0, "r_info_sym": 1, "r_info_type": 2}]),
        Section(".symtab", kind="SHT_SYMTAB", symbols=[{"st_shndx": 2}, {"st_shndx": 0}]),
    ]
    monkeypatch.setattr(ownership, "ELFFile", lambda stream: SimpleNamespace(iter_sections=lambda: iter(sections)))
    got = ownership.emitted(b"object")
    assert got[".rodata"] == (word(1, 2, 0x34), {0: True}, [(4, 8 + 8)])  # .rdata starts at 8; the addend is 8
    assert got[".data"] == (word(7), {}, [])
    assert set(got) == {".rodata", ".data"}


# ------------------------------------------------------------------ where the bytes live


def test_locate_a_unique_range(monkeypatch):
    rom = bytes(0x10) + word(0x3F800000, 0x40000000) + word(9)
    rows = [(0x10, 0x14, "a", ".rodata", 0x800C0000), (0x14, 0x18, "b", ".rodata", 0x800C0004),
            (0x18, 0x1C, "c", ".rodata", 0x800C0008)]
    emits(monkeypatch, **{".rodata": (word(0x3F800000, 0x40000000), {}, [])})
    assert ownership.locate(index(rom, rows), b"", None) == {".rodata": [[0x10, 0x18]]}


def test_locate_finds_bytes_that_start_inside_a_row_and_explains_what_is_nowhere(monkeypatch):
    rom = word(5, 6, 7)
    emits(monkeypatch, **{".rodata": (word(6, 7), {}, [])})
    assert ownership.locate(index(rom, [(0, 12, "wide", ".rodata", 0x80000000)]), b"", None) == {
        ".rodata": [[4, 12]]}
    emits(monkeypatch, **{".rodata": (word(9, 9), {}, [])})
    assert ownership.locate(index(rom, [(0, 12, "w", ".rodata", 0)]), b"", None) == {
        ".rodata": {"why": "bytes found nowhere in the ROM at the unit's text", "at": ""}}


def test_locate_a_jump_table_into_the_units_own_text_at_the_address_the_link_gives(monkeypatch):
    # func_80207738_de: five relocated words, the ROM holds the text address, the object only the offsets
    offsets = (0x34, 0x2C, 0x2C, 0x24, 0x84)
    rom = word(*(TEXT_VRAM + o for o in offsets))
    emits(monkeypatch, **{".rodata": (word(*offsets), {4 * i: True for i in range(5)}, [])})
    rows = [(0, 20, "table", ".rodata", 0x800C1B88)]
    assert ownership.locate(index(rom, rows), b"", (TEXT_VRAM, TEXT_ROM))[".rodata"] == [[0, 20]]


def test_locate_a_jump_table_that_points_elsewhere_is_unplaced(monkeypatch):
    rom = word(0x00207770, 0x00207768)  # eight bytes further than the object's offsets say
    emits(monkeypatch, **{".rodata": (word(0x34, 0x2C), {0: True, 4: True}, [])})
    reason = ownership.locate(index(rom, [(0, 8, "t", ".rodata", 0x80000000)]), b"", (TEXT_VRAM, TEXT_ROM))
    assert reason[".rodata"]["why"].startswith("label table absent from the ROM")


def test_locate_narrows_equal_constants_by_the_addresses_the_code_forms(monkeypatch):
    one = word(0x3F800000)
    # the unit's code at ROM 8: lui $a0, 0x800C; addiu $a0, $a0, 4 - the constant lives at exactly 0x800C0004
    rom = one + one + word(0x3C04800C, 0x24840004)
    rows = [(0, 4, "first", ".rodata", 0x800C1000), (4, 8, "second", ".rodata", 0x800C0004)]
    emits(monkeypatch, **{".rodata": (one, {}, [(4, 0)])})
    assert ownership.locate(index(rom, rows), b"", (TEXT_VRAM, 8))[".rodata"] == [[4, 8]]
    emits(monkeypatch, **{".rodata": (one, {}, [])})  # without a reference the range nearest to the code stands
    assert ownership.locate(index(rom, rows), b"", (TEXT_VRAM, 8))[".rodata"] == [[4, 8]]
    rom = bytes(8) + one + bytes(12) + one + bytes(8)  # code at 16: equally near on both sides, both stay candidates
    rows = [(8, 12, "first", ".rodata", 0x800C1000), (24, 28, "second", ".rodata", 0x800C2000)]
    assert ownership.locate(index(rom, rows), b"", (TEXT_VRAM, 16))[".rodata"] == [[8, 12], [24, 28]]


def test_locate_trusts_the_address_the_code_forms_over_a_row_that_holds_the_same_bytes(monkeypatch):
    one = word(0x3F800000)
    rom = one + word(5) + one + word(6) + word(0x3C04800C, 0x24840004)  # the code forms 0x800C0004: inside a row
    rows = [(0, 4, "elsewhere", ".rodata", 0x800C1000), (4, 16, "wide", ".rodata", 0x800C0000)]
    emits(monkeypatch, **{".rodata": (one, {}, [(4, 0)])})
    assert ownership.locate(index(rom, rows), b"", (TEXT_VRAM, 16))[".rodata"] == [[8, 12]]


def test_locate_picks_the_equal_constant_nearest_to_the_code_when_nothing_else_tells(monkeypatch):
    one = word(0x3F800000)
    rom = one + word(5, 6, 7) + one + bytes(12)
    rows = [(0, 4, "far", ".rodata", 0x800C0000), (16, 20, "near", ".rodata", 0x800C0100)]
    emits(monkeypatch, **{".rodata": (one, {}, [])})
    assert ownership.locate(index(rom, rows), b"", (TEXT_VRAM, 24))[".rodata"] == [[16, 20]]


def test_locate_names_the_runs_when_the_code_places_the_bytes_in_several_places(monkeypatch):
    """func_802362E8_de: two functions' pools, 28 foreign bytes between them: no ROM holds the 64 bytes as one run."""
    rom = word(1, 2, 7, 7, 7, 3, 4) + word(0x3C04800C, 0x24840000, 0x3C04800C, 0x24840014)
    rows = [(0, 8, "first", ".rodata", 0x800C0000), (8, 20, "foreign", ".rodata", 0x800C0008),
            (20, 28, "second", ".rodata", 0x800C0014)]
    emits(monkeypatch, **{".rodata": (word(1, 2, 3, 4), {}, [(4, 0), (12, 8)])})
    got = ownership.locate(index(rom, rows), b"", (TEXT_VRAM, 28))[".rodata"]
    assert got == {"why": "the code places the bytes in 2 separate runs of the ROM, not one", "at": ""}


# ------------------------------------------------------------------ what a unit then owns


def member(name, kind="data", section=".rodata", holders=("a",)):
    return Member(name, kind, "asm", "g", tuple(Placement(v, section, 0, 4, 0x100, 0) for v in holders))


MEMBERS = {"f": member("f", "function", ".text"), "bss": member("bss", section=".bss"),
           "old1": member("old1"), "old2": member("old2"), "row": member("row"), "labels": member("labels")}
# rom 0..12 as three whole rows that follow each other in memory
ROWS = [(0, 4, "old1", ".rodata", 0x100), (4, 8, "row", ".rodata", 0x104), (8, 12, "old2", ".rodata", 0x108)]
INDEXES = {"a": index(bytes(12), ROWS), "b": index(bytes(12), ROWS)}


def unit(*names, **kw):
    return UnitSpec("src/f.c", "c", "g", names, "tc", {"add": [], "omit": []}, **kw)


def resolve(u, result, names_from="a"):
    return ownership._resolve(u, MEMBERS, result, INDEXES, names_from)


def test_resolve_keeps_code_and_bss_and_names_each_claim_by_section_unit_and_address():
    keep, resolved, claims, debt = resolve(unit("f", "bss", "old1", "old2"), {"a": {".rodata": [[4, 12]]}})
    assert (keep, resolved, debt) == (["f", "bss"], {"a"}, [])
    assert claims == [Claim("src/f.c", "a", ".rodata", 4, 12, ("rodata/f/00000104",))]


def test_resolve_names_a_claim_by_the_names_from_holders_address_else_the_first():
    both = {"a": {".rodata": [[0, 4]]}, "b": {".rodata": [[4, 8]]}}
    assert {c.rows for c in resolve(unit("f"), both)[2]} == {("rodata/f/00000100",)}
    assert {c.rows for c in resolve(unit("f"), both, "b")[2]} == {("rodata/f/00000104",)}
    assert {c.rows for c in resolve(unit("f"), both, "c")[2]} == {("rodata/f/00000100",)}


def test_resolve_drops_what_the_object_no_longer_emits():
    """func_80202CA0_de: the layout owned 20 bytes of rodata, the C compiles none."""
    keep, resolved, claims, debt = resolve(unit("f", "old1"), {"a": {}})
    assert (keep, resolved, claims, debt) == (["f"], {"a"}, [], [])


def test_resolve_withholds_a_holder_whose_data_cannot_be_placed():
    reason = {"why": "bytes found nowhere in the ROM at the unit's text", "at": ""}
    _, resolved, _, debt = resolve(unit("f", "old1"), {"a": {".data": reason}, "b": {}})
    assert resolved == {"b"} and debt == ["bytes found nowhere in the ROM at the unit's text: src/f.c a .data"]


def test_resolve_claims_bytes_that_end_inside_a_row_because_the_rows_follow_the_claim():
    _, resolved, claims, debt = resolve(unit("f"), {"a": {".rodata": [[2, 8]]}})
    assert resolved == {"a"} and debt == [] and [(c.start, c.end) for c in claims] == [(2, 8)]


def test_resolve_names_a_claim_that_is_not_rows_following_each_other_in_memory():
    rows = [(0, 4, "old1", ".rodata", 0x100), (4, 8, "row", ".rodata", 0x900)]  # a gap in memory
    _, resolved, _, debt = ownership._resolve(unit("f"), MEMBERS, {"a": {".rodata": [[0, 8]]}},
                                              {"a": index(bytes(8), rows)}, "a")
    assert resolved == set() and debt == [
        "bytes are not whole rows that follow each other in memory: src/f.c a .rodata @0x0-0x8"]


def test_resolve_picks_among_ambiguous_ranges_by_the_previous_owner_else_names_them():
    options = [[4, 8], [0, 4]]
    assert [(c.start, c.end) for c in resolve(unit("f", "old1"), {"a": {".rodata": options}})[2]] == [(0, 4)]
    _, resolved, _, debt = resolve(unit("f"), {"a": {".rodata": options}})
    assert resolved == set() and debt == ["ambiguous: src/f.c a .rodata among 2 ranges 0x4-0x8, 0x0-0x4"]


def test_resolve_withholds_a_holder_where_the_unit_does_not_compile():
    _, resolved, _, debt = resolve(unit("f", "old1"), {"a": "preprocess.error: boom"})
    assert resolved == set() and debt == ["compile failed (preprocess.error): src/f.c a boom"]


def test_own_names_the_rows_of_the_kept_claims_and_withholds_the_failing_holders():
    members = {"f": member("f", "function", ".text", ("a", "b", "c"))}
    found = [Claim("src/f.c", "a", ".rodata", 0, 4, ("r",)), Claim("src/f.c", "b", ".rodata", 8, 12, ("r",))]
    taken = defaultdict(list)
    result, kept = ownership._own(unit("f"), members, ["f"], {"a", "b"}, found, taken, [])  # holder c is withheld
    assert (result.members, result.withheld, kept) == (("f", "r"), ("c",), found)
    assert taken == {"a": [(0, 4, "src/f.c")], "b": [(8, 12, "src/f.c")]}


def test_own_withholds_the_holder_that_wants_bytes_another_unit_owns_in_that_version_only():
    members = {"f": member("f", "function", ".text", ("a", "b"))}
    taken, debt = defaultdict(list, {"a": [(2, 6, "src/other.c")]}), []
    found = [Claim("src/f.c", "a", ".rodata", 4, 8, ("r",)), Claim("src/f.c", "b", ".rodata", 4, 8, ("r",))]
    result, kept = ownership._own(unit("f"), members, ["f"], {"a", "b"}, found, taken, debt)
    assert result.withheld == ("a",) and kept == found[1:] and result.members == ("f", "r")
    assert debt == ["row owned by another unit: src/f.c a (claimed by src/other.c)"]


@pytest.mark.parametrize("start, end, owner", [
    (0, 2, None), (0, 4, "x"), (3, 5, "x"), (6, 7, "y"), (4, 6, None), (0, 20, "x"), (7, 9, "y"), (8, 9, None)])
def test_clash_names_the_unit_that_holds_some_of_the_bytes(start, end, owner):
    assert ownership._clash([(2, 4, "x"), (6, 8, "y")], start, end) == owner


def test_claims_give_bytes_to_one_unit_only_and_aggregate_the_debt(monkeypatch):
    missing = {"why": "not in the ROM", "at": ""}
    monkeypatch.setattr(ownership.pool, "map", lambda cfg, name, fn, items, key=None: [
        {"a": {".rodata": [[4, 8]]}}, {"a": {".rodata": [[4, 8]]}}, {"a": {".rodata": missing}}])
    monkeypatch.setattr(ownership, "_index", lambda snapshot, v: INDEXES["a"])
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["compile"]}}})
    found = {}

    @contextmanager
    def stage(name):
        yield SimpleNamespace(add=lambda **kw: found.update(kw))

    monkeypatch.setattr(ownership.effort, "stage", stage)
    units = {p: replace(unit("f"), path=p) for p in ("src/a.c", "src/b.c", "src/c.c")}
    snapshot = SimpleNamespace(config=SimpleNamespace(project=SimpleNamespace(names_from="a")),
                               layout=SimpleNamespace(members=MEMBERS, units=units), versions={"a": None})
    out, claims, findings = ownership.claims(snapshot)
    assert findings == found["findings"]
    assert [out[p].members for p in units] == [("f", "rodata/a/00000104"), ("f",), ("f",)]  # b.c lost them to a.c
    assert [out[p].withheld for p in units] == [(), ("a",), ("a",)]
    assert claims == (Claim("src/a.c", "a", ".rodata", 4, 8, ("rodata/a/00000104",)),)
    assert [f.reason for f in found["findings"]] == [
        "1 unit holders are withheld: not in the ROM",
        "1 unit holders are withheld: row owned by another unit"]
    assert not found["findings"][0].blocking and found["items"] == 3


def test_exact_withholds_the_holders_whose_bytes_do_not_reproduce_the_rom(monkeypatch):
    members = {"f": member("f", "function", ".text", ("a", "b"))}
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["compile"]}}})
    seen = []
    gaps = ["", ".text is 0x28 bytes"]
    monkeypatch.setattr(ownership.pool, "map", lambda cfg, name, fn, items, key=None: seen.extend(items) or gaps)
    found = {}

    @contextmanager
    def stage(name):
        yield SimpleNamespace(add=lambda **kw: found.update(kw))

    monkeypatch.setattr(ownership.effort, "stage", stage)
    snapshot = Snapshot(None, "h", LayoutMap(200, {}, members, {}, "d", (), {}), {}, {}, "d")
    out, findings = ownership.exact(snapshot, {"src/f.c": unit("f", withheld=("c",))})
    assert [(u.path, v) for _, u, v in seen] == [("src/f.c", "a"), ("src/f.c", "b")]  # a withheld holder is not proved
    assert out["src/f.c"].withheld == ("b", "c")
    assert "not exact" in found["findings"][0].reason and findings == found["findings"]


def test_landed_c_that_does_not_compile_refuses_while_the_ownership_classes_stay_debt():
    found = ownership._findings({
        "compile failed (compile.error)": ["src/f.c a boom"], "compile failed (symbols.unknown)": ["src/g.c b x"],
        "label table absent from the ROM": ["src/h.c a .data"]}, "fix it")
    assert [(f.key, f.blocking) for f in found] == [
        ("compile.error", True), ("layout.ownership", False), ("layout.ownership", False)]
    assert found[0].reason == "1 unit holders do not compile: compile failed (compile.error)"
    assert found[0].missing == ("src/f.c a boom",) and found[0].action == "fix it"


def test_derive_gives_a_candidate_the_rows_its_source_emits(monkeypatch):
    from unbake import layout
    members = {"f": member("f", "function", ".text"), "row": member("row")}
    snapshot = SimpleNamespace(config=SimpleNamespace(project=SimpleNamespace(names_from="a")),
                               layout=SimpleNamespace(members=members, units={}), versions={"a": None})
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["compile"]}}})
    monkeypatch.setattr(ownership, "_index", lambda snap, v: INDEXES["a"])
    monkeypatch.setattr(ownership, "_job", lambda item: {"a": {".rodata": [[4, 8]]}})
    monkeypatch.setattr(layout, "claim_rows", lambda snap, claims: {})  # the rows are the claims already
    shown, got = ownership.derive(snapshot, unit("f"))
    assert shown is snapshot and got.members == ("f", "rodata/f/00000104") and got.withheld == ()
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["assemble"]}}})
    assert ownership.derive(snapshot, unit("f")) == (snapshot, unit("f"))  # not compiled: as it is


def test_derive_returns_a_data_unit_as_it_is(monkeypatch):
    snapshot = SimpleNamespace(layout=SimpleNamespace(members={"row": member("row")}, units={}))
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["compile"]}}})
    assert ownership.derive(snapshot, unit("row")) == (snapshot, unit("row"))


def test_derive_generates_the_rows_in_a_private_copy_and_takes_other_units_rows_as_taken(monkeypatch):
    from unbake import layout
    members = {"f": member("f", "function", ".text"), "mine": member("mine"), "theirs": member("theirs")}
    other = replace(unit("theirs"), path="src/g.c")
    snapshot = SimpleNamespace(config=SimpleNamespace(project=SimpleNamespace(names_from="a")),
                               layout=LayoutMap(200, {}, members, {"src/g.c": other}, "d", (), {}),
                               versions={"a": None})
    copy = SimpleNamespace(marker="copy")
    seen = {}
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["compile"]}}})
    monkeypatch.setattr(ownership, "_index", lambda snap, v: INDEXES["a"])
    monkeypatch.setattr(ownership, "_job", lambda item: {"a": {".rodata": [[8, 12], [0, 4]][:1]}})
    monkeypatch.setattr(layout, "claim_rows", lambda snap, claims: {"split.yaml": b"rows"})
    monkeypatch.setattr(layout, "dump_map", lambda value: seen.setdefault("units", value.units) and b"map")
    monkeypatch.setattr(layout, "overlay", lambda snap, writes: seen.setdefault("writes", writes) and copy)
    shown, got = ownership.derive(snapshot, unit("f"))
    assert shown is copy and seen["writes"] == {"split.yaml": b"rows", "layout.toml": b"map"}
    assert seen["units"]["src/f.c"] == got and "src/g.c" in seen["units"]
    assert got.members == ("f", "rodata/f/00000108") and got.withheld == ()


def test_derive_does_not_reserve_another_units_unclaimed_holder(monkeypatch):
    from unbake import layout
    members = {"f": member("f", "function", ".text", ("b",)),
               "rodata/g/00000100": member("rodata/g/00000100", holders=("a",))}
    other = replace(unit("rodata/g/00000100"), path="src/g.c")
    snapshot = SimpleNamespace(config=SimpleNamespace(project=SimpleNamespace(names_from="a")),
                               layout=LayoutMap(200, {}, members, {other.path: other}, "d", (), {}),
                               versions={"a": None, "b": None})
    monkeypatch.setattr(ownership.config, "load_resource", lambda name: {"kind": {"c": {"phases": ["compile"]}}})
    monkeypatch.setattr(ownership, "_index", lambda snap, v: INDEXES[v])
    monkeypatch.setattr(ownership, "_job", lambda item: {"b": {".rodata": [[0, 4]]}})
    claims = []
    monkeypatch.setattr(layout, "claim_rows", lambda snap, found: claims.extend(found) or {})
    shown, got = ownership.derive(snapshot, unit("f"))
    assert shown is snapshot and got.withheld == ()
    assert claims == [Claim("src/f.c", "b", ".rodata", 0, 4, ("rodata/f/00000100",))]
    assert got.members == ("f", "rodata/f/00000100")
