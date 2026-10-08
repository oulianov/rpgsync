"""Database tables (RPG_RT.ldb) <-> typed Python files."""

import ast
import glob
import os
import re
import subprocess
import sys

import pytest
from conftest import GAMES, TESTGAME

from rpgsync import database as D
from rpgsync import db as M
from rpgsync import tables as T
from rpgsync.commands import SIZE_NOTE, CompileError, Ctx
from rpgsync.dbschema import STRUCTS
from rpgsync.lcf import LcfFile, Reader, decode_value, encode_value, read_struct, write_struct
from rpgsync.project import Project

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_GAMES = sorted(glob.glob(os.path.join(ROOT, "..", "Les Secrets*FRQ")))
ALL_GAMES = GAMES + REAL_GAMES
GAME_2003 = os.path.join(TESTGAME, "TestGame-2003")
GAME_2000 = os.path.join(TESTGAME, "TestGame-2000")
EDIT_GAMES = [g for g in [GAME_2000, GAME_2003] + REAL_GAMES if os.path.isdir(g)]


def _ids(games):
    return [os.path.basename(g)[:20] for g in games]


def _load(game, tmp_path):
    """(ctx, ldb bytes).  Nothing is written into the game folder."""
    ctx = Project(game, script_dir=str(tmp_path / "scripts")).context()
    with open(os.path.join(game, "RPG_RT.ldb"), "rb") as f:
        return ctx, f.read()


def _class_body(text):
    """The statements of the table class of a database file."""
    return next(st for st in ast.parse(text).body if isinstance(st, ast.ClassDef)).body


def _entry_id(stmt):
    """Id of the entry assigned by `stmt` (positional or keyword), else None."""
    if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    first = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "id"), None)
    return first.value if isinstance(first, ast.Constant) else None


def _entry_lines(text, entry_id):
    """(first, last) line numbers of the `attr = Model(...)` statement of the entry with `entry_id`."""
    for stmt in _class_body(text):
        if _entry_id(stmt) == entry_id:
            return stmt.lineno, stmt.end_lineno
    raise AssertionError("entry %d not found" % entry_id)


def _append_entry(text, src):
    """Add `src` (an `attr = Model(...)` statement) at the end of the class body."""
    end = _class_body(text)[-1].end_lineno
    lines = text.split("\n")
    return "\n".join(lines[:end] + ["    " + line for line in src.split("\n")] + lines[end:])


def _src(table, *body):
    """A database file in the class layout; `body` are the lines of the
    class, so the first one is line 2."""
    cls, base = D.table_class(table)
    lines = "".join("    %s\n" % line for line in body) or "    pass\n"
    return "class %s(%s):\n%s\n\n%s = %s()\n" % (cls, base, lines, table, cls)


def _to_legacy(text, name):
    """The same table in the list layout of older rpgsync versions."""
    calls = [ast.get_source_segment(text, st.value) for st in _class_body(text) if _entry_id(st) is not None]
    size = D.table_size(text)
    return '"""legacy"""\nfrom rpgsync.db import *\n\n%s%s = [\n%s]\n' % (
        "size = %d\n" % size.size if size else "",
        name,
        "".join("    %s,\n" % c for c in calls),
    )


def _save_and_reload(db, tmp_path):
    path = str(tmp_path / "RPG_RT.ldb")
    db.save(path)
    return LcfFile.load(path)


def _db_chunks(f):
    return {c.id: c.raw for c in LcfFile.parse(f.to_bytes()).root.chunks}


def _items(f, name):
    return LcfFile.parse(f.to_bytes()).root.get(name)


# --------------------------------------------------------------------------
# (a) lossless round trips
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dbgame", ALL_GAMES, ids=_ids(ALL_GAMES))
def test_tables_roundtrip(dbgame, tmp_path):
    """export -> compile -> apply reproduces RPG_RT.ldb byte for byte."""
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data)
    texts = D.export_tables(db.root, ctx)
    assert list(texts) == [n for n in D.TABLES if D.has_table(db.root, n)]
    assert "actors" in texts and "switches" in texts
    if D.db_engine(db.root) == "2k3":
        assert "classes" in texts
    db2 = LcfFile.parse(data)
    for name, text in texts.items():
        entries = D.compile_table(name, text, ctx)
        assert D.table_size(text).size == len(db2.root.get(name))
        assert [e.key() for e in entries] == [e.key() for e in D.table_entries_from_bin(db2.root, name, ctx)], name
        summary = D.apply_table(db2.root, name, entries, ctx)
        assert not any(summary.values()), (name, summary)
    assert db2.to_bytes() == data
    # exporting again gives the same text (deterministic)
    assert D.export_tables(db2.root, ctx) == texts


@pytest.mark.parametrize("dbgame", ALL_GAMES, ids=_ids(ALL_GAMES))
def test_chunk_codecs_are_exact(dbgame, tmp_path):
    """Every typed chunk of every table decodes and re-encodes to the same bytes."""
    _, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data).root

    def check(st):
        for c in st.chunks:
            kind = st._field(c.id)[2]
            if kind == "raw":
                continue
            v = decode_value(kind, c.raw)
            assert encode_value(kind, v) == c.raw, (st.name, hex(c.id))
            if kind.startswith("S:"):
                check(v)
            elif kind.startswith("A:"):
                for it in v:
                    check(it.struct)

    for name in D.TABLES:
        if D.has_table(db, name):
            for it in db.get(name):
                check(it.struct)


@pytest.mark.parametrize("dbgame", ALL_GAMES, ids=_ids(ALL_GAMES))
def test_entries_rebuild_from_scratch(dbgame, tmp_path):
    """Encoding a model into a brand new entry yields the same model: checks
    the encoders on all real data, not only the 'unchanged' path."""
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data).root
    engine = D.db_engine(db)
    for name in D.TABLES:
        sname = D.table_struct(name)
        for e in D.table_entries_from_bin(db, name, ctx):
            st = D.new_struct(sname, engine)
            D.patch_struct(st, sname, e, ctx, engine)
            back = D.struct_to_model(read_struct(Reader(write_struct(st)), sname), sname, ctx, e.id)
            assert back.key() == e.key(), (name, e.id)


# --------------------------------------------------------------------------
# (b) edits
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dbgame", EDIT_GAMES, ids=_ids(EDIT_GAMES))
def test_edit_enemy_max_hp(dbgame, tmp_path):
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data)
    text = D.export_tables(db.root, ctx)["enemies"]
    entries = D.compile_table("enemies", text, ctx)
    enemy = next(e for e in entries if e.max_hp != 10)
    lo, hi = _entry_lines(text, enemy.id)
    lines = text.split("\n")
    seg = "\n".join(lines[lo - 1 : hi])
    new_seg, n = re.subn(r"\bmax_hp=\d+", "max_hp=4321", seg)
    assert n == 1
    text2 = "\n".join(lines[: lo - 1] + [new_seg] + lines[hi:])

    summary = D.apply_table(db.root, "enemies", D.compile_table("enemies", text2, ctx), ctx)
    assert summary == {"added": [], "removed": [], "changed": [enemy.id]}
    f = _save_and_reload(db, tmp_path)
    orig = LcfFile.parse(data)

    before, after = _db_chunks(orig), _db_chunks(f)
    assert before.keys() == after.keys()
    assert [k for k in before if before[k] != after[k]] == [0x0E]
    old_items, new_items = orig.root.get("enemies"), f.root.get("enemies")
    assert [it.id for it in new_items] == list(range(1, len(old_items) + 1))
    for old, new in zip(old_items, new_items):
        if old.id != enemy.id:
            assert write_struct(old.struct) == write_struct(new.struct)
            continue
        assert [c.id for c in old.struct.chunks] == [c.id for c in new.struct.chunks]
        for oc, nc in zip(old.struct.chunks, new.struct.chunks):
            assert (oc.raw == nc.raw) == (oc.id != 0x04), hex(oc.id)
        assert new.struct.get("max_hp") == 4321
    # the file still describes the game
    assert [e.key() for e in D.compile_table("enemies", text2, ctx)] == [
        e.key() for e in D.table_entries_from_bin(f.root, "enemies", ctx)
    ]


@pytest.mark.parametrize("dbgame", EDIT_GAMES, ids=_ids(EDIT_GAMES))
def test_add_item(dbgame, tmp_path):
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data)
    engine = D.db_engine(db.root)
    count = len(db.root.get("items"))
    new_id = count + 2  # leaves a gap at count + 1
    text = D.export_tables(db.root, ctx)["items"]
    assert text.endswith("\n\n\nitems = Items()\n")
    text2 = _append_entry(
        text,
        'elixir = Item(%d, "Elixir \\u00e9", description="Heals all.", type="medicine",\n'
        "    price=999, recover_hp_rate=100, recover_sp_rate=100, entire_party=True)" % new_id,
    )

    entries = D.compile_table("items", text2, ctx)
    assert (entries[-1].id, entries[-1]._ident) == (new_id, "elixir")
    summary = D.apply_table(db.root, "items", entries, ctx)
    assert summary == {"added": [new_id], "removed": [], "changed": [], "size": [new_id - 2, new_id]}
    f = _save_and_reload(db, tmp_path)
    orig = LcfFile.parse(data)

    before, after = _db_chunks(orig), _db_chunks(f)
    assert [k for k in before if before[k] != after[k]] == [0x0D]
    items = f.root.get("items")
    assert [it.id for it in items] == list(range(1, new_id + 1))
    for old, new in zip(orig.root.get("items"), items):
        assert write_struct(old.struct) == write_struct(new.struct)
    gap = items[count]
    assert write_struct(gap.struct) == write_struct(D.new_struct("Item", engine))
    item = D.struct_to_model(items[-1].struct, "Item", ctx, new_id)
    assert (item.name, item.description, item.type, item.price) == ("Elixir é", "Heals all.", "medicine", 999)
    assert (item.recover_hp_rate, item.recover_sp_rate, item.entire_party) == (100, 100, True)
    assert item.uses == 1 and item.hit == 90  # liblcf defaults
    if engine == "2k3":
        assert len(item.animation_data) == 1  # what the 2003 editor writes for a new item
    else:
        assert items[-1].struct._chunk(0x46) is None  # no 2003 chunks in a 2000 database
    # read back, the new entry is listed (the gap is not)
    listed = [e.id for e in D.table_entries_from_bin(f.root, "items", ctx)]
    assert new_id in listed and count + 1 not in listed


@pytest.mark.parametrize("dbgame", EDIT_GAMES, ids=_ids(EDIT_GAMES))
def test_remove_skill(dbgame, tmp_path):
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data)
    engine = D.db_engine(db.root)
    text = D.export_tables(db.root, ctx)["skills"]
    entries = D.compile_table("skills", text, ctx)
    victim = entries[len(entries) // 2].id
    lo, hi = _entry_lines(text, victim)
    lines = text.split("\n")
    text2 = "\n".join(lines[: lo - 1] + lines[hi:])

    summary = D.apply_table(db.root, "skills", D.compile_table("skills", text2, ctx), ctx)
    assert summary == {"added": [], "removed": [victim], "changed": []}
    f = _save_and_reload(db, tmp_path)
    orig = LcfFile.parse(data)

    before, after = _db_chunks(orig), _db_chunks(f)
    assert [k for k in before if before[k] != after[k]] == [0x0C]
    old_items, new_items = orig.root.get("skills"), f.root.get("skills")
    assert [it.id for it in new_items] == list(range(1, len(old_items) + 1))
    actors = len(orig.root.get("actors"))
    for old, new in zip(old_items, new_items):
        if old.id == victim:
            assert write_struct(new.struct) == write_struct(D.new_struct("Skill", engine, actors))
        else:
            assert write_struct(old.struct) == write_struct(new.struct)
    assert victim not in [e.id for e in D.table_entries_from_bin(f.root, "skills", ctx)]


def test_new_entries_match_editor_layout(tmp_path):
    """New/empty entries are laid out like the RPG Maker 2003 editor writes them."""
    if not REAL_GAMES:
        pytest.skip("real game not found")
    _, data = _load(REAL_GAMES[0], tmp_path)
    db = LcfFile.parse(data).root
    for name in ("actors", "items", "terrains", "switches", "variables"):
        last = db.get(name)[-1]
        assert write_struct(last.struct) == write_struct(D.new_struct(D.table_struct(name), "2k3")), name


def test_new_actor_keeps_editor_defaults(tmp_path):
    ctx, data = _load(GAME_2003, tmp_path)
    db = LcfFile.parse(data)
    n = len(db.root.get("actors")) + 1
    text = D.export_tables(db.root, ctx)["actors"]
    text2 = _append_entry(text, 'bob = Actor(%d, "Bob", final_level=20)' % n)
    D.apply_table(db.root, "actors", D.compile_table("actors", text2, ctx), ctx)
    bob = D.struct_to_model(db.root.get("actors")[-1].struct, "Actor", ctx, n)
    assert bob.name == "Bob" and bob.final_level == 20
    assert bob.parameters.maxhp == [1] * 99 and bob.parameters.maxsp == [0] * 99
    assert bob.battle_commands == [-1] * 7


def test_change_nested_values(tmp_path):
    """Enums, nested structs, arrays and bit sets are patched in place."""
    ctx, data = _load(GAME_2003, tmp_path)
    db = LcfFile.parse(data)
    entries = D.table_entries_from_bin(db.root, "enemies", ctx)
    e = entries[0]
    e.actions[0].kind = "skill"
    e.actions[0].skill_id = 3
    e.actions.append(M.EnemyAction(basic="escape", rating=7))
    e.state_ranks[0] = 4
    summary = D.apply_table(db.root, "enemies", entries, ctx)
    assert summary["changed"] == [e.id]
    f = _save_and_reload(db, tmp_path)
    back = D.table_entries_from_bin(f.root, "enemies", ctx)
    D._normalize(e, "Enemy")
    assert [x.key() for x in back] == [x.key() for x in entries]
    assert back[0].actions[-1].id == len(back[0].actions)


def test_refuses_holes(tmp_path):
    ctx, data = _load(GAME_2003, tmp_path)
    db = LcfFile.parse(data)
    entries = D.table_entries_from_bin(db.root, "items", ctx)
    db.root.get("items")[1].id = 7
    with pytest.raises(ValueError):
        D.apply_table(db.root, "items", entries, ctx)


def test_classes_need_2003(tmp_path):
    ctx, data = _load(GAME_2000, tmp_path)
    db = LcfFile.parse(data)
    assert "classes" not in D.export_tables(db.root, ctx)
    assert D.apply_table(db.root, "classes", [], ctx) == {"added": [], "removed": [], "changed": []}
    with pytest.raises(ValueError):
        D.apply_table(db.root, "classes", [M.Class(id=1, name="Knight")], ctx)


# --------------------------------------------------------------------------
# Layout: one class per table, attribute names, legacy lists
# --------------------------------------------------------------------------


def test_rendered_layout():
    text = D.render_table("items", [M.Item(id=1, name="Potion", price=50)], size=200)
    assert text.endswith(
        "from rpgsync.tables import *\n\n\n"
        "class Items(Table):\n"
        "    class Config:\n"
        "        size = 200  # %s\n\n"
        '    potion = Item(id=1, name="Potion", price=50)\n\n\n'
        "items = Items()\n" % SIZE_NOTE
    )
    # an empty table is still a class
    empty = D.render_table("items", [])
    assert "class Items(Table):\n    pass\n\n\nitems = Items()\n" in empty
    assert D.compile_table("items", empty, Ctx()) == []


def test_variables_are_positional():
    entries = [M.Variable(id=5, name=""), M.Variable(id=32, name="Code voulu"), M.Variable(id=33, name="Code voulu")]
    text = D.render_table("variables", entries, size=50)
    assert "\nclass Variables(VariableTable):\n" in text and text.endswith("\nvariables = Variables()\n")
    assert '\n    variable_5 = Variable(5, "")\n' in text
    assert '\n    code_voulu = Variable(32, "Code voulu")\n' in text
    assert '\n    code_voulu_33 = Variable(33, "Code voulu")\n' in text
    back = D.compile_table("variables", text, Ctx())
    assert [(e.id, e.name, e._ident) for e in back] == [
        (5, "", "variable_5"),
        (32, "Code voulu", "code_voulu"),
        (33, "Code voulu", "code_voulu_33"),
    ]
    assert [e.key() for e in back] == [e.key() for e in entries]
    assert D.table_size(text).size == 50
    assert D.read_identifiers(text) == {5: "variable_5", 32: "code_voulu", 33: "code_voulu_33"}
    assert D.render_table("variables", back, size=50, keep=D.read_identifiers(text)) == text
    # the file runs: the attribute is the entry
    ns = {}
    exec(compile(text, "variables.py", "exec"), ns)
    attr = ns["Variables"].code_voulu
    assert type(attr) is T.Variable and isinstance(attr, M.Variable)
    assert attr.key() == T.Variable(32, "Code voulu").key() == M.Variable(id=32, name="Code voulu").key()
    # same for switches
    text = D.render_table("switches", [M.Switch(id=12, name="Night mode")])
    assert "\nclass Switches(SwitchTable):\n" in text and '\n    night_mode = Switch(12, "Night mode")\n' in text
    assert text.endswith("\nswitches = Switches()\n")
    [sw] = D.compile_table("switches", text, Ctx())
    assert (sw.id, sw.name, sw._ident) == (12, "Night mode", "night_mode")


@pytest.mark.parametrize("dbgame", EDIT_GAMES, ids=_ids(EDIT_GAMES))
def test_game_variables_are_positional(dbgame, tmp_path):
    ctx, data = _load(dbgame, tmp_path)
    texts = D.export_tables(LcfFile.parse(data).root, ctx)
    for name, model in (("variables", "Variable"), ("switches", "Switch")):
        body = [st for st in _class_body(texts[name]) if _entry_id(st) is not None]
        assert body, name
        for st in body:
            assert st.value.func.id == model and len(st.value.args) == 2 and not st.value.keywords


def test_identifiers():
    names = {32: "Code voulu", 33: "", 34: "Code voulu", 35: "class", 36: "3D", 37: "size", 38: "Élan vital"}
    assert D.identifiers("variables", names) == {
        32: "code_voulu",
        33: "variable_33",  # unusable name: <entry>_<id>
        34: "code_voulu_34",  # clash: _<id>
        35: "variable_35",  # keyword
        36: "variable_36",  # starts with a digit
        37: "variable_37",  # the size of the table
        38: "elan_vital",
    }
    assert D.identifiers("enemies", {1: ""}) == {1: "enemy_1"}
    assert D.identifiers("battleranimations", {1: ""}) == {1: "battler_animation_1"}
    # names in the file win; unusable or duplicate ones are ignored; ids no longer listed are dropped
    keep = {1: "not valid", 2: "potion", 3: "gone", 4: "class", 5: "potion", 6: "size"}
    names = {1: "Potion", 2: "Ether", 4: "Elixir", 5: "Tent", 6: "Hi-Potion"}
    assert D.identifiers("items", names, keep) == {
        1: "potion_1",
        2: "potion",
        4: "elixir",
        5: "tent",
        6: "hi_potion",
    }
    # the _<id> suffix may be taken too: names stay unique
    idents = D.identifiers("items", {1: "Potion", 2: "Potion 3", 3: "Potion"})
    assert idents == {1: "potion", 2: "potion_3", 3: "potion_3_2"}
    idents = D.identifiers("items", {1: "Potion", 2: "Ether", 3: "Potion"}, keep={2: "potion_3"})
    assert len(set(idents.values())) == 3 and idents[2] == "potion_3"
    items = [M.Item(id=n, name=v) for n, v in sorted({1: "Potion", 2: "Potion 3", 3: "Potion"}.items())]
    assert [e._ident for e in D.compile_table("items", D.render_table("items", items), Ctx())] == [
        "potion",
        "potion_3",
        "potion_3_2",
    ]


def test_read_identifiers():
    text = _src(
        "items",
        "size = 9",
        'potion = Item(1, "Potion")',
        "ether: Item = Item(id=2)",
        'elixir = Item(name="Elixir")',  # no id: nothing to keep
        "bad = Item(id=-3)",
    )
    assert D.read_identifiers(text) == {1: "potion"}  # annotated or negative ids are not read
    assert D.read_identifiers("items = [\n    Item(id=1),\n]\n") == {}  # legacy entries have no name
    assert D.read_identifiers("class Items(Table):\n    potion = Item(id=1,\n") == {}  # syntax error


def test_reexport_keeps_attribute_names(tmp_path):
    """Attribute names belong to the scripts: re-exporting the table (after
    the entry was renamed in the editor) keeps the names of the file."""
    ctx, data = _load(GAME_2003, tmp_path)
    db = LcfFile.parse(data)
    text = D.export_tables(db.root, ctx)["items"]
    entries = D.compile_table("items", text, ctx)
    entry = entries[1]
    old = entry._ident
    assert text.count("\n    %s = Item(" % old) == 1
    edited = text.replace("\n    %s = Item(" % old, "\n    my_potion = Item(")
    keep = {"items": D.read_identifiers(edited)}
    assert keep["items"][entry.id] == "my_potion"
    # nothing else changed: re-exporting gives the edited file back
    assert D.export_tables(db.root, ctx, keep=keep)["items"] == edited
    assert D.export_tables(db.root, ctx)["items"] == text  # without keep, the derived name
    # rename the entry in the database: the attribute keeps its name
    entry.name = "Renamed Potion"
    assert D.apply_table(db.root, "items", entries, ctx)["changed"] == [entry.id]
    texts = D.export_tables(db.root, ctx, keep=keep)
    assert '\n    my_potion = Item(\n        id=%d,\n        name="Renamed Potion",\n' % entry.id in texts["items"]
    assert "\n    renamed_potion = Item(" not in texts["items"]
    back = {e.id: e for e in D.compile_table("items", texts["items"], ctx)}
    assert (back[entry.id]._ident, back[entry.id].name) == ("my_potion", "Renamed Potion")
    assert "\n    renamed_potion = Item(" in D.export_tables(db.root, ctx)["items"]
    # keep only touches its own table
    assert {n: t for n, t in texts.items() if n != "items"} == {
        n: t for n, t in D.export_tables(db.root, ctx).items() if n != "items"
    }


@pytest.mark.parametrize("dbgame", ALL_GAMES, ids=_ids(ALL_GAMES))
def test_legacy_list_layout(dbgame, tmp_path):
    """Files written by older rpgsync versions (a top-level list) compile to
    the same entries as the class layout."""
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data)
    for name, text in D.export_tables(db.root, ctx).items():
        legacy = _to_legacy(text, name)
        assert "class " not in legacy
        new, old = D.compile_table(name, text, ctx), D.compile_table(name, legacy, ctx)
        assert [e.key() for e in old] == [e.key() for e in new], name
        assert all(e._ident is None for e in old)
        assert all(e._ident is not None for e in new)
        assert D.table_size(legacy).size == D.table_size(text).size
        assert not any(D.apply_table(db.root, name, old, ctx).values()), name
    assert db.to_bytes() == data


def test_legacy_list_details():
    src = '"""Items"""\nfrom rpgsync.db import *\nsize = 5\nitems = [\n    Item(id=2, name="B"),\n    Item(id=1),\n]\n'
    a, b = D.compile_table("items", src, Ctx())
    assert (a.id, a.name, a.lineno, a._ident) == (2, "B", 5, None)
    assert (b.id, b.lineno) == (1, 6)
    assert D.table_size(src).size == 5
    assert D.compile_table("items", "items = []\n", Ctx()) == []


def test_entry_without_id():
    text = _src("items", 'elixir = Item(name="Elixir")', 'potion = Item(1, "Potion")', "tent = Item()")
    elixir, potion, tent = D.compile_table("items", text, Ctx())
    assert (elixir.id, elixir.name, elixir._ident, elixir.lineno) == (None, "Elixir", "elixir", 2)
    assert (potion.id, potion._ident) == (1, "potion")
    assert (tent.id, tent._ident) == (None, "tent")
    text = _src("variables", 'code = Variable(name="Code")', "spare = Variable()")
    code, spare = D.compile_table("variables", text, Ctx())
    assert (code.id, code.name, code._ident) == (None, "Code", "code")
    assert (spare.id, spare.name, spare._ident) == (None, "", "spare")


def test_duplicate_attribute():
    text = _src("enemies", "slime = Enemy(", "    id=1,", ")", "bat = Enemy(id=2)", "slime = Enemy(id=3)")
    with pytest.raises(CompileError) as exc:
        D.compile_table("enemies", text, Ctx())
    assert "slime is defined twice (also on line 2)" in str(exc.value)
    assert exc.value.lineno == 6


def test_empty_tables():
    for src in (_src("enemies"), _src("enemies", '"""No enemies yet."""'), _src("enemies", "size = 3")):
        assert D.compile_table("enemies", src, Ctx()) == []
    assert D.table_size(_src("enemies", "size = 3")).size == 3
    assert D.table_size(_src("enemies")) is None


# --------------------------------------------------------------------------
# Compile errors
# --------------------------------------------------------------------------

BAD = [
    ("enemies", _src("enemies", "a = Enemy(id=1, max_hpp=5)"), 2, "unknown field"),
    ("enemies", _src("enemies", "a = Enemy(id=1)", "b = Enemy(id=1)"), 3, "id 1 is used twice (also on line 2)"),
    ("enemies", _src("enemies", "a = Enemy(id=1, max_hp=-5)"), 2, "max_hp: -5 is out of range"),
    (
        "classes",
        _src(
            "classes",
            "a = Class(id=1, parameters=Parameters(maxhp=[1, 2], maxsp=[0, 0], attack=[1, 1],"
            " defense=[1, 1], spirit=[1, 1], agility=[1, 0]))",
        ),
        2,
        "agility: 0 is out of range",
    ),
    ("skills", _src("skills", 'a = Skill(id=1, scope="everyone")'), 2, "scope: Input should be"),
    ("enemies", _src("enemies", "a = Enemy(id=0)"), 2, "positive"),
    ("enemies", _src("enemies", "a = Enemy(-1)"), 2, "positive"),
    ("enemies", _src("enemies", 'a = Enemy(id=1, max_hp="lots")'), 2, "max_hp"),
    ("enemies", _src("enemies", 'a = Enemy(id=1, actions=[EnemyAction(kind="dance")])'), 2, "kind"),
    ("enemies", _src("enemies", "a = Enemy(id=1, actions=[Learning(level=2)])"), 2, "actions"),
    ("enemies", _src("enemies", 'a = Enemy(id=1, name="日本")'), 2, "encoding"),
    ("enemies", _src("enemies", "a = Enemy(id=1, state_ranks=[300])"), 2, "out of range"),
    ("enemies", _src("enemies", "a = Enemy(id=1, max_hp=2**40)"), 2, "supported"),
    ("enemies", _src("enemies", "a = Enemy(id=1, max_hp=x)"), 2, "unknown name"),
    ("enemies", _src("enemies", 'a = Enemy(1, "x", 5)'), 2, "only id and name can be given without a keyword"),
    ("enemies", _src("enemies", "a = Enemy(*ids)"), 2, "without a keyword"),
    ("enemies", _src("enemies", "a = Enemy(1, id=1)"), 2, "id given twice"),
    ("enemies", _src("enemies", "a = Enemy(**kw)"), 2, "**kwargs"),
    ("enemies", _src("enemies", "a = Actor(id=1)"), 2, "must be Enemy"),
    ("enemies", _src("enemies", "a = make_enemy(1)"), 2, "unknown call"),
    ("enemies", _src("enemies", "a = Enemy(id=1)", "a = Enemy(id=2)"), 3, "a is defined twice (also on line 2)"),
    ("enemies", _src("enemies", "size = 3", "size = 4"), 3, "size is assigned twice"),
    ("enemies", _src("enemies", "size = -1"), 2, "size must be"),
    ("enemies", _src("enemies", "print(1)"), 2, "may only contain"),
    ("enemies", _src("enemies", "def f(self):", "    pass"), 2, "may only contain"),
    ("enemies", _src("enemies", "a, b = Enemy(id=1), Enemy(id=2)"), 2, "may only contain"),
    ("enemies", "import os\nprint(1)\n" + _src("enemies"), 2, "top level"),
    ("enemies", _src("enemies") + "enemies = Other()\n", 6, "top level"),
    ("enemies", _src("enemies") + "enemies = [\n    Enemy(id=1),\n]\n", 6, "top level"),  # class and list
    ("enemies", _src("enemies") + "class Bosses(Table):\n    pass\n", 6, "one class"),
    ("enemies", "class Enemies(Table):\n    a = Enemy(id=1,\n", 2, "syntax"),
    ("enemies", "x = []", 1, "top level"),
    ("enemies", '"""doc"""', None, "missing `class Enemies(Table):`"),  # neither a class nor a list
    ("enemies", '"""doc"""\nfrom rpgsync.tables import *\nsize = 4\n', None, "missing"),
    ("variables", "", None, "missing `class Variables(VariableTable):`"),
    # legacy list layout: same checks, same lines
    ("enemies", "enemies = [\n    Enemy(id=1, max_hpp=5),\n]", 2, "unknown field"),
    ("enemies", "enemies = [\n    Enemy(id=1),\n    Enemy(id=1),\n]", 3, "used twice"),
    ("enemies", "import os\nprint(1)\nenemies = []", 2, "top level"),
]


@pytest.mark.parametrize("table,src,line,msg", BAD, ids=["%d-%s" % (i, b[3][:30]) for i, b in enumerate(BAD)])
def test_compile_errors(table, src, line, msg):
    with pytest.raises(CompileError) as exc:
        D.compile_table(table, src, Ctx(encoding="cp1252"))
    assert msg in str(exc.value)
    assert exc.value.lineno == line


def test_compile_accepts_python_forms():
    src = '''"""Items"""
from rpgsync.tables import *


class Items(Table):
    """My items."""

    size = 10
    first: Item = Item(2, type=1, actor_set=[True] * 3, animation_data=[BattlerAnimationItemSkill()] * 2,
                       attribute_set=(False, True))
    second = Item(id=1, name="A", atk_points1=-1 if False else 5) if False else Item(id=1, name="A")


items = Items()
'''
    with pytest.raises(CompileError):  # conditional expressions are not literals
        D.compile_table("items", src, Ctx())
    src = src.replace(' if False else Item(id=1, name="A")', "").replace("-1 if False else 5", "-5")
    a, b = D.compile_table("items", src, Ctx())
    assert (a.id, a._ident, a.lineno) == (2, "first", 9)
    assert a.type == "weapon"  # enum numbers become names
    assert a.actor_set == [True, True, True]
    assert [x.id for x in a.animation_data] == [1, 2]  # element ids default to positions
    assert a.attribute_set == [False, True]
    assert (b.id, b.atk_points1, b.lineno, b._ident) == (1, -5, 11, "second")
    assert D.table_size(src).size == 10


# --------------------------------------------------------------------------
# (c) generated files are Python, the models validate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dbgame", ALL_GAMES, ids=_ids(ALL_GAMES))
def test_files_are_importable_python(dbgame, tmp_path):
    """The files run as Python against rpgsync.tables, and what they define
    is what compile_table reads."""
    ctx, data = _load(dbgame, tmp_path)
    db = LcfFile.parse(data)
    for name, text in D.export_tables(db.root, ctx).items():
        assert "\nfrom rpgsync.tables import *\n" in text and "rpgsync.db" not in text
        ns = {}
        exec(compile(text, "%s.py" % name, "exec"), ns)
        cls_name, base = D.table_class(name)
        table = ns[cls_name]
        assert issubclass(table, getattr(T, base)) and isinstance(ns[name], table)
        assert table.Config.size == D.table_size(text).size == len(db.root.get(name))
        model = D.MODEL_CLASSES[D.table_struct(name)]
        values = {k: v for k, v in vars(table).items() if isinstance(v, model)}
        compiled = D.compile_table(name, text, ctx)
        assert list(values) == [c._ident for c in compiled], name
        for v, c in zip(values.values(), compiled):
            v = model.model_validate(v.model_dump())  # `[X()] * n` shares one instance
            D._normalize(v, D.table_struct(name))
            assert v.key() == c.key()


def test_models_match_schema():
    """Model defaults are liblcf's; every exported field has a description."""
    for sname, cls in D.MODEL_CLASSES.items():
        assert cls.__doc__
        if sname not in STRUCTS:
            continue
        for f in D._fields(sname):
            info = cls.model_fields[f.name]
            assert info.description, (sname, f.name)
            d = info.get_default(call_default_factory=True)
            if f.default != f.default_2k3:
                assert d is None
            elif f.model in ("int", "bool", "str") or f.model.startswith("enum:"):
                assert d == f.default, (sname, f.name)


def test_generated_files_up_to_date(tmp_path):
    csv_dir = os.path.join(ROOT, "..", "Player", "lib", "liblcf", "generator", "csv")
    if not os.path.isdir(csv_dir):
        pytest.skip("liblcf CSVs not found")
    subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "tools", "gen_dbschema.py"),
            "--csv-dir",
            csv_dir,
            "--out-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
    )
    for name in ("dbschema.py", "db.py"):
        with (
            open(os.path.join(ROOT, "rpgsync", name), encoding="utf-8") as a,
            open(tmp_path / name, encoding="utf-8") as b,
        ):
            assert a.read() == b.read(), "%s is out of date: run tools/gen_dbschema.py" % name
