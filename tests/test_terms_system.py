"""The terms, the system settings and the string variables of RPG_RT.ldb <-> database files."""

import ast
import os

import pytest
from conftest import TESTGAME

from rpgsync import database as D
from rpgsync import db as M
from rpgsync import dsl
from rpgsync import tables as T
from rpgsync.commands import CompileError
from rpgsync.lcf import Chunk, LcfFile
from rpgsync.project import Project
from rpgsync.sync import SingleUnit, Syncer

GAME_2003 = os.path.join(TESTGAME, "TestGame-2003")
GAME_MANIAC = os.path.join(TESTGAME, "TestGame-Maniac")


def _load(game, tmp_path):
    """(ctx, ldb bytes).  Nothing is written into the game folder."""
    ctx = Project(game, script_dir=str(tmp_path / "scripts")).context()
    with open(os.path.join(game, "RPG_RT.ldb"), "rb") as f:
        return ctx, f.read()


def _need(game):
    if not os.path.isdir(game):
        pytest.skip("%s not found" % os.path.basename(game))


def _subchunks(data, name):
    return [(c.id, c.raw) for c in LcfFile.parse(data).root.get(name).chunks]


def _top_chunks(data):
    return {c.id: c.raw for c in LcfFile.parse(data).root.chunks}


def _apply(data, name, text, ctx):
    """`data` with the single structure `name` set from the file `text`; (bytes, summary)."""
    f = LcfFile.parse(data)
    summary = D.apply_single(f.root, name, D.single_entries(name, D.compile_single(name, text, ctx)), ctx)
    return f.to_bytes(), summary


# --------------------------------------------------------------------------
# Round trips
# --------------------------------------------------------------------------


def test_singles_roundtrip(game, tmp_path):
    """export -> compile -> apply reproduces RPG_RT.ldb byte for byte."""
    ctx, data = _load(game, tmp_path)
    db = LcfFile.parse(data)
    texts = D.export_singles(db.root, ctx)
    assert list(texts) == ["terms", "system"]
    db2 = LcfFile.parse(data)
    for name, text in texts.items():
        model = D.compile_single(name, text, ctx)
        entries = D.single_entries(name, model)
        assert [e.key() for e in entries] == [
            e.key() for e in D.single_entries(name, D.single_from_bin(db2.root, name, ctx))
        ]
        assert D.apply_single(db2.root, name, entries, ctx) == {}
    assert db2.to_bytes() == data
    assert D.export_singles(db2.root, ctx) == texts  # deterministic


def test_singles_are_importable_python(game, tmp_path):
    """The files run as Python against rpgsync.tables, and define what compile_single reads."""
    ctx, data = _load(game, tmp_path)
    for name, text in D.export_singles(LcfFile.parse(data).root, ctx).items():
        assert "\nfrom rpgsync.tables import *\n" in text
        ns = {}
        exec(compile(text, "%s.py" % name, "exec"), ns)
        value = ns[name]
        assert isinstance(value, M.DbModel) and type(value).__name__ == D.single_struct(name)
        D._normalize(value, D.single_struct(name))
        assert value.key() == D.compile_single(name, text, ctx).key()


def test_save_count_stays_out_of_system(tmp_path):
    """The editor counts its saves in System: not shown, and a save does not change the file."""
    _need(GAME_2003)
    ctx, data = _load(GAME_2003, tmp_path)
    f = LcfFile.parse(data)
    text = D.export_singles(f.root, ctx)["system"]
    assert "save_count" not in text
    before = D.single_fingerprint(f.root, "system")
    system = f.root.get("system")
    system.set("save_count", b"\x63")  # kept raw: not exported
    f.root.mark_dirty("system")
    g = LcfFile.parse(f.to_bytes())
    assert D.single_fingerprint(g.root, "system") == before
    assert D.export_singles(g.root, ctx)["system"] == text


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------


def test_edit_term_changes_only_its_chunk(tmp_path):
    _need(GAME_2003)
    ctx, data = _load(GAME_2003, tmp_path)
    text = D.export_singles(LcfFile.parse(data).root, ctx)["terms"]
    old = next(ln for ln in text.splitlines() if ln.startswith("    victory="))
    new_data, summary = _apply(data, "terms", text.replace(old, '    victory="Bravo !",'), ctx)
    assert summary == {"changed": ["victory"]}
    before, after = _top_chunks(data), _top_chunks(new_data)
    assert [k for k in before if before[k] != after[k]] == [0x15]
    a, b = dict(_subchunks(data, "terms")), dict(_subchunks(new_data, "terms"))
    assert [k for k in a if a[k] != b[k]] == [0x05] and b[0x05] == b"Bravo !"
    assert [k for k, _ in _subchunks(data, "terms")] == [k for k, _ in _subchunks(new_data, "terms")]


def test_left_out_field_is_the_default(tmp_path):
    """A field removed from the file goes back to its default (an empty term)."""
    _need(GAME_2003)
    ctx, data = _load(GAME_2003, tmp_path)
    text = D.export_singles(LcfFile.parse(data).root, ctx)["terms"]
    line = next(ln for ln in text.splitlines() if ln.startswith("    defeat="))
    new_data, summary = _apply(data, "terms", text.replace(line + "\n", ""), ctx)
    assert summary == {"changed": ["defeat"]}
    assert dict(_subchunks(new_data, "terms"))[0x06] == b""


def test_edit_system_party_counts_members(tmp_path):
    """The size chunk of the starting party counts actors (liblcf Count<>), not bytes."""
    _need(GAME_2003)
    ctx, data = _load(GAME_2003, tmp_path)
    text = D.export_singles(LcfFile.parse(data).root, ctx)["system"]
    tree = ast.parse(text)
    call = next(st.value for st in tree.body if isinstance(st, ast.Assign))
    party = next(k for k in call.keywords if k.arg == "party")
    text = text.replace(ast.get_source_segment(text, party), "party=[3, 1, 2]")
    text = text.replace('title_music=Music(name="', 'title_music=Music(volume=80, name="', 1)
    new_data, summary = _apply(data, "system", text, ctx)
    assert summary == {"changed": ["party", "title_music"]}
    system = LcfFile.parse(new_data).root.get("system")
    assert list(system.get("party")) == [3, 1, 2] and system.get(0x15) == 3
    assert system.get("title_music").get("volume") == 80
    before, after = _top_chunks(data), _top_chunks(new_data)
    assert [k for k in before if before[k] != after[k]] == [0x16]


def test_unknown_chunks_survive(tmp_path):
    """Chunks liblcf doesn't name (the Maniac Patch's, here) stay byte for byte, in place."""
    _need(GAME_MANIAC)
    ctx, data = _load(GAME_MANIAC, tmp_path)
    f = LcfFile.parse(data)
    terms = f.root.get("terms")
    terms.chunks.insert(3, Chunk(0xF0, b"\x01\x02unknown"))  # one more, between known ones
    f.root.mark_dirty("terms")
    data = f.to_bytes()
    text = D.export_singles(LcfFile.parse(data).root, ctx)["terms"]
    line = next(ln for ln in text.splitlines() if ln.startswith("    victory="))
    new_data, _ = _apply(data, "terms", text.replace(line, '    victory="!",'), ctx)
    old, new = _subchunks(data, "terms"), _subchunks(new_data, "terms")
    assert [k for k, _ in old] == [k for k, _ in new]
    known = {f.id for f in D._fields("Terms")}
    unknown = [(k, raw) for k, raw in old if k not in known]
    assert {0xAB, 0xAC, 0xF0} <= {k for k, _ in unknown}
    assert unknown == [(k, raw) for k, raw in new if k not in known]


@pytest.mark.parametrize(
    ("src", "line", "message"),
    [
        ('terms = Terms(victory="x")\nterms = Terms()\n', 2, "assigned twice"),
        ("x = 1\n", 1, "only holds `terms = Terms"),
        ("terms = System()\n", 1, "must be Terms"),
        ('terms = Terms(nope="x")\n', 1, "nope: unknown field"),
        ('"""only a docstring"""\n', None, "missing `terms = Terms"),
        ("terms = Terms(victory=3)\n", 1, "victory"),
    ],
)
def test_compile_errors(src, line, message):
    with pytest.raises(CompileError, match=message) as e:
        D.compile_single("terms", src, D.Ctx())
    assert e.value.lineno == line


# --------------------------------------------------------------------------
# String variables (Maniac Patch)
# --------------------------------------------------------------------------


def test_string_variables_table(tmp_path):
    """database/strings.py: positional entries like the variables; a new name is written."""
    _need(GAME_MANIAC)
    ctx, data = _load(GAME_MANIAC, tmp_path)
    db = LcfFile.parse(data)
    assert D.has_table(db.root, "strings")
    text = D.export_tables(db.root, ctx, tables=["strings"])["strings"]
    assert "class Strings(StringTable):" in text and text.rstrip().endswith("strings = Strings()")
    text = text.replace(
        "\n\n\nstrings = Strings()", '\n\n    hero_name = StringVariable(2, "Hero name")\n\n\nstrings = Strings()'
    )
    entries = D.compile_table("strings", text, ctx)
    assert [(e.id, e.name, e._ident) for e in entries] == [(2, "Hero name", "hero_name")]
    summary = D.apply_table(db.root, "strings", entries, ctx)
    assert summary["added"] == [2]
    after = LcfFile.parse(db.to_bytes()).root
    assert [it.struct.get("name") for it in after.get("strings")][:2] == [b"", b"Hero name"]
    assert _top_chunks(db.to_bytes()).keys() == _top_chunks(data).keys()
    again = D.export_tables(after, ctx, tables=["strings"])["strings"]
    assert '    hero_name = StringVariable(2, "Hero name")' in again


def test_string_variable_names_avoid_the_table_methods():
    names = {1: "Hero name", 2: "keys", 3: "Size"}
    out = D.identifiers("strings", names)
    assert out[1] == "hero_name"
    assert out[3] == "string_variable_3"  # reserved
    assert set(out.values()).isdisjoint(set(dir(dsl.StringVars)) | {"size", "Config"})
    assert issubclass(T.StringTable, dsl.StringVars)


def test_no_string_table_without_the_patch(tmp_path):
    _need(GAME_2003)
    _, data = _load(GAME_2003, tmp_path)
    assert not D.has_table(LcfFile.parse(data).root, "strings")


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


def _units(game):
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    return syncer, {u.name: u for u in syncer.units()}


def _edit(path, old, new):
    text = open(path, encoding="utf-8").read()
    assert old in text, old
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))


def _set_term(path, field, value: bytes):
    f = LcfFile.load(path)
    f.root.get("terms").set(field, value)
    f.root.mark_dirty("terms")
    f.save(path)


def test_sync_units(exported2003):
    syncer, units = _units(exported2003)
    unit = units["database/terms"]
    assert isinstance(unit, SingleUnit) and isinstance(units["database/system"], SingleUnit)
    assert os.path.exists(unit.py_path) and unit.py_path.endswith(os.path.join("database", "terms.py"))
    victory = LcfFile.load(unit.bin_path).root.get("terms").get("victory").decode("cp1252")
    _edit(unit.py_path, 'victory="%s"' % victory, 'victory="Gagné !"')
    r = syncer.sync(unit)
    assert r.action == "import" and "changed field victory" in r.message, r.message
    assert LcfFile.load(unit.bin_path).root.get("terms").get("victory") == "Gagné !".encode("cp1252")
    # the other units sharing RPG_RT.ldb see nothing new
    assert all(syncer.sync(u).action == "none" for u in syncer.units())
    # the editor changes a term: the file follows
    _set_term(unit.bin_path, "defeat", b"Perdu...")
    assert syncer.sync(unit).action == "export"
    assert 'defeat="Perdu..."' in open(unit.py_path, encoding="utf-8").read()
    assert all(syncer.sync(u).action == "none" for u in syncer.units())


def test_both_sides_merge_field_by_field(exported2003):
    syncer, units = _units(exported2003)
    unit = units["database/terms"]
    terms = LcfFile.load(unit.bin_path).root.get("terms")
    victory, defeat = (terms.get(k).decode("cp1252") for k in ("victory", "defeat"))
    _edit(unit.py_path, 'victory="%s"' % victory, 'victory="Script"')
    _set_term(unit.bin_path, "defeat", b"Editor")
    r = syncer.sync(unit)
    assert r.action == "merge", r.message
    terms = LcfFile.load(unit.bin_path).root.get("terms")
    assert (terms.get("victory"), terms.get("defeat")) == (b"Script", b"Editor")
    text = open(unit.py_path, encoding="utf-8").read()
    assert 'victory="Script"' in text and 'defeat="Editor"' in text
    # the same field on both sides: the editor's wins, the script is kept aside
    _edit(unit.py_path, 'victory="Script"', 'victory="Mine"')
    _set_term(unit.bin_path, "victory", b"Theirs")
    r = syncer.sync(unit)
    assert r.action == "conflict" and "CONFLICT on field victory" in r.message, r.message
    assert LcfFile.load(unit.bin_path).root.get("terms").get("victory") == b"Theirs"
