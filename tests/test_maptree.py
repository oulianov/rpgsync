"""The map tree: RPG_RT.lmt <-> database/map_tree.py, map names in scripts
(``maps.relm``), and the checks of map references."""

import glob
import os
import re

import pytest
from typer.testing import CliRunner

from rpgsync import cli
from rpgsync import db as M
from rpgsync.commands import CompileError, Ctx
from rpgsync.lcf import LcfFile, MapTree
from rpgsync.maptree import (
    StartSpec,
    TreeOrder,
    blank_map,
    compile_map_tree,
    identifiers,
    map_reference_problems,
    map_type,
    tree_order,
    tree_problems,
)
from rpgsync.project import Project
from rpgsync.sync import MapTreeUnit, Syncer


def _syncer(game):
    return Syncer(Project(str(game)), log=lambda *_: None)


def _tree_unit(syncer):
    return next(u for u in syncer.units() if isinstance(u, MapTreeUnit))


def _sync_all(syncer):
    return {u.name: syncer.sync(u) for u in syncer.units()}


def _text(path):
    return open(path, encoding="utf-8").read()


def _edit(path, old, new):
    text = _text(path)
    assert old in text, old
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))


def _entry_line(text, ident):
    return re.search(r"^ *%s = .*\n" % ident, text, re.M).group(0)


# -- RPG_RT.lmt ------------------------------------------------------------------


def test_map_tree_roundtrip(game):
    """Read and written back byte for byte, also when every chunk is re-encoded."""
    data = open(os.path.join(game, "RPG_RT.lmt"), "rb").read()
    tree = MapTree.parse(data)
    assert tree.to_bytes() == data
    for it in tree.maps:
        for c in it.struct.chunks:
            it.struct.get(c.id)
            c.dirty = True
    for c in tree.start.chunks:
        tree.start.get(c.id)
        c.dirty = True
    assert tree.to_bytes() == data
    assert LcfFile.parse(data).to_bytes() == data  # the raw form write_bin checks


def test_export_compiles_to_the_same_tree(game):
    project = Project(game, script_dir=os.path.join(game, "__unused__"))
    unit = MapTreeUnit(project)
    data = open(project.lmt_path, "rb").read()
    text = unit.decompile(data)
    assert [s.key() for s in unit.compile(text)] == [s.key() for s in unit.specs_from_bin(data)]
    tree = MapTree.parse(data)
    applied = MapTree.parse(data)
    from rpgsync.maptree import apply_map_tree

    changes = apply_map_tree(applied, unit.compile(text), project.context(), project.context().engine)
    assert changes.summary == {"added": [], "removed": [], "changed": []}
    assert applied.tree_order == tree.tree_order


def test_tree_order_is_depth_first():
    parents = {1: 0, 2: 1, 3: 0, 4: 2, 5: 1, 6: 99}  # 6: parent missing, listed last
    assert tree_order(parents, {5: 0, 2: 1}, {0}) == [1, 5, 2, 4, 3, 6]


def test_identifiers():
    names = {1: "Relm (1)", 2: "Start", 3: "Relm (1)", 4: "", 5: "2nd floor", 6: "class"}
    out = identifiers(names, {3: "relm_cellar"})
    assert out == {1: "relm_1", 2: "map_2", 3: "relm_cellar", 4: "map_4", 5: "map_5", 6: "map_6"}


def test_blank_map_reads_back():
    m = LcfFile.parse(blank_map()).root
    assert (m.get("chipset_id"), m.get("width"), m.get("height"), m.get("events")) == (1, 20, 15, [])
    assert len(m._chunk(0x47).raw) == len(m._chunk(0x48).raw) == 600  # 20x15 int16 tiles


# -- map_tree.py -------------------------------------------------------------------


def test_exported_file(exported2003):
    text = _text(os.path.join(exported2003, "Scripts", "database", "map_tree.py"))
    assert "class Maps(MapTree):" in text and text.rstrip().endswith("maps = Maps()")
    assert re.search(r"\n    start = Start\(", text)
    assert '    map_events = Map(\n        id=1,\n        name="Map events",' in text
    assert "    with map_events:\n        picture_events = Map(" in text
    assert (
        '        generate_dungeon = Map(id=4, name="Generate Dungeon")\n'
        "        with generate_dungeon:\n"
        '            dungeon_road = Map(id=3, name="Dungeon Road")\n'
    ) in text
    assert "parent_map" not in text and "# under" not in text
    assert "area_test = Area(" in text and "area_rect=[16, 1, 19, 4]" in text
    assert not re.search(r"\btype=", text)


def _compile(src, entries="", start=""):
    text = "from rpgsync.tables import *\n\n\nclass Maps(MapTree):\n%s\n%s\n\nmaps = Maps()\n" % (start, entries)
    return compile_map_tree(text, Ctx(encoding="cp1252"))


def test_compile_entries():
    specs = _compile(
        "",
        '    town = Map(id=1, name="Town")\n'
        "    with town:\n"
        '        house = Map(id=3, name="House")\n'
        "        with house:\n"
        '            cellar = Area(id=2, name="Cellar", area_rect=[1, 2, 3, 4])\n'
        '        shop = Map(id=4, name="Shop")\n'
        '    world = Map(id=5, name="World")\n',
        "    start = Start(party_map_id=1, party_x=3, party_y=4)",
    )
    entries = [s for s in specs if isinstance(s, M.MapInfo)]
    assert [(e._ident, e.id, e.type, e.parent_map) for e in entries] == [
        ("town", 1, "map", 0),
        ("house", 3, "map", 1),
        ("cellar", 2, "area", 3),
        ("shop", 4, "map", 1),
        ("world", 5, "map", 0),
    ]
    order = next(s for s in specs if isinstance(s, TreeOrder))
    assert order.order == [1, 3, 2, 4, 5] and not order.flat
    start = next(s for s in specs if isinstance(s, StartSpec))
    assert (start.start.party_map_id, start.start.party_x, start.lineno) == (1, 3, 5)


@pytest.mark.parametrize(
    "entry, message",
    [
        ('town = Map(id=1, type="area")', "takes no type"),
        ("town = Area(id=1, area_rect=[1, 2])", "area_rect is [left, top, right, bottom]"),
        ("town = Area(id=1, area_rect=[5, 2, 1, 4])", "right < left"),
        ("town = Map(id=0)", "0 is the root"),
        ("town = Variable(1)", "Map(...) or Area(...)"),
        ("town = Map(id=1, name=3)", "name"),
        ("town = Map(id=1)\n    with nowhere:\n        a = Map(id=2)", "nowhere is not a map of the tree"),
        ("town = Map(id=1)\n    b = Map(id=2)\n    with town:\n        a = Map(id=3)", "goes right after `town = Map"),
        (
            "town = Map(id=1)\n    with town:\n        a = Map(id=2)\n    with town:\n        b = Map(id=3)",
            "town already has its `with` block, on line 7",
        ),
        ("cave = Area(id=1)\n    with cave:\n        a = Map(id=2)", "cave is an area: no map goes under an area"),
        ("town = Map(id=1)\n    with town:\n        a = Map(id=2, parent_map=7)", "its parent: remove parent_map="),
        ("town = Map(id=1)\n    with town:\n        start = Start()", "start goes on the top level"),
        ("town = Map(id=1)\n    with town as t:\n        a = Map(id=2)", "`with name:` holds the maps under"),
        ("town = Map(id=1)\n    with town:\n        town = Map(id=2)", "town is defined twice"),
    ],
)
def test_compile_errors(entry, message):
    with pytest.raises(CompileError, match=re.escape(message)):
        _compile("", "    " + entry)


def test_compile_flat_file_of_an_older_rpgsync():
    specs = _compile(
        "",
        '    town = Map(id=1, name="Town")\n'
        '    house = Map(id=3, name="House", parent_map=1)\n'
        '    shop = Map(id=4, name="Shop", parent_map=1)\n'
        '    lost = Map(id=6, name="Lost", parent_map=-1)\n',
    )
    entries = [s for s in specs if isinstance(s, M.MapInfo)]
    assert [(e.id, e.parent_map) for e in entries] == [(1, 0), (3, 1), (4, 1), (6, -1)]
    order = next(s for s in specs if isinstance(s, TreeOrder))
    assert order.order == [1, 3, 4, 6] and order.flat
    # a parent that is not listed (a broken tree) is not a flat file
    order = next(s for s in _compile("", "    lost = Map(id=6, parent_map=-1)") if isinstance(s, TreeOrder))
    assert not order.flat


def test_compile_duplicates():
    with pytest.raises(CompileError, match="used twice"):
        _compile("", "    a = Map(id=1)\n    b = Map(id=1)")
    with pytest.raises(CompileError, match="defined twice"):
        _compile("", "    a = Map(id=1)\n    a = Map(id=2)")


def test_rename_a_map_retitles_its_script(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    _edit(unit.py_path, 'name="Dungeon Road"', 'name="Long Road"')
    r = syncer.sync(unit)
    assert r.action == "import" and "changed map 3" in r.message
    assert MapTree.load(unit.bin_path).info(3).get("name") == b"Long Road"
    results = _sync_all(syncer)
    assert "renamed in the map tree" in results["Map0003"].message
    assert _text(syncer.project.script_for_map(3)).startswith('"""Map0003 - Long Road"""\n')
    assert all(r.action == "none" for r in _sync_all(syncer).values())


def test_add_a_map(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    _edit(unit.py_path, "        battle_test = Map(", '        forest = Map(name="Forest")\n        battle_test = Map(')
    r = syncer.sync(unit)
    assert r.action == "import" and "created Map0022.lmu" in r.message, r.message
    assert '\n        forest = Map(id=22, name="Forest")\n        battle_test = Map(' in _text(unit.py_path)
    tree = MapTree.load(unit.bin_path)
    info = tree.info(22)
    assert info.get("name") == b"Forest" and info.get("parent_map") == 1 and map_type(tree.maps[-1]) == 1
    assert info._chunk(0x03).raw == b"\x02"  # indentation: under a top-level map
    assert tree.tree_order.index(22) == tree.tree_order.index(4) + 5  # after Generate Dungeon and its 4 maps
    # the chunks the editor writes for a new map
    assert [c.id for c in info.chunks] == [0x01, 0x02, 0x03, 0x04, 0x0B, 0x0C, 0x15, 0x1F, 0x20, 0x21, 0x29, 0x33]
    assert open(os.path.join(exported2003, "Map0022.lmu"), "rb").read() == blank_map()
    results = _sync_all(syncer)
    assert results["Map0022"].action == "export"
    # event scripts can teleport there by name
    script = syncer.project.script_for_map(2)
    _edit(script, "teleport(maps.picture_events, 13, 2", "teleport(maps.forest, 5, 6")
    results = _sync_all(syncer)
    assert results["Map0002"].action == "import"
    cmds = [
        c
        for it in LcfFile.load(os.path.join(exported2003, "Map0002.lmu")).root.get("events")
        for pg in it.struct.get("pages")
        for c in pg.struct.get("event_commands")
    ]
    assert any(c.code == 10810 and list(c.params[:3]) == [22, 5, 6] for c in cmds)


def test_add_maps_under_a_new_map(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    new = (
        '        forest = Map(name="Forest")\n'
        "        with forest:\n"
        '            clearing = Map(name="Clearing")\n'
        "            with clearing:\n"
        '                hut = Map(name="Hut")\n'
        '            glade = Area(name="Glade", area_rect=[1, 1, 4, 4])\n'
    )
    _edit(unit.py_path, "        battle_test = Map(", new + "        battle_test = Map(")
    r = syncer.sync(unit)
    assert r.action == "import" and "created Map0022.lmu, Map0023.lmu, Map0024.lmu" in r.message, r.message
    tree = MapTree.load(unit.bin_path)
    parents = {mid: tree.info(mid).get("parent_map") for mid in (22, 23, 24, 25)}
    assert parents == {22: 1, 23: 22, 24: 23, 25: 22}
    assert [tree.info(mid)._chunk(0x03).raw for mid in (22, 23, 24)] == [b"\x02", b"\x03", b"\x04"]
    i = tree.tree_order.index(22)
    assert tree.tree_order[i : i + 5] == [22, 23, 24, 25, 5]
    text = _text(unit.py_path)
    assert (
        '        forest = Map(id=22, name="Forest")\n'
        "        with forest:\n"
        '            clearing = Map(id=23, name="Clearing")\n'
        "            with clearing:\n"
        '                hut = Map(id=24, name="Hut")\n'
        '            glade = Area(id=25, name="Glade", area_rect=[1, 1, 4, 4])\n'
    ) in text
    assert not os.path.exists(os.path.join(exported2003, "Map0025.lmu"))  # an area has no file
    assert all(r.action in ("none", "export") for r in _sync_all(syncer).values())
    assert all(r.action == "none" for r in _sync_all(syncer).values())


def test_new_top_level_map_gets_the_editor_settings(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    _edit(unit.py_path, "\n\nmaps = Maps()", '\n    world = Map(name="World")\n\n\nmaps = Maps()')
    assert syncer.sync(unit).action == "import"
    text = _text(unit.py_path)
    assert 'world = Map(\n        id=22,\n        name="World",\n        music_type="event",' in text
    assert 'teleport="allow"' in _entry_line(text, "world") or 'teleport="allow"' in text.split("world = Map(")[1]


def test_delete_a_map(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    script = syncer.project.script_for_map(8)
    _edit(unit.py_path, _entry_line(_text(unit.py_path), "dungeon_obstacle"), "")
    r = syncer.sync(unit)
    assert r.action == "import" and "removed map 8" in r.message and "deleted Map0008.lmu and Map0008.py" in r.message
    assert not os.path.exists(os.path.join(exported2003, "Map0008.lmu")) and not os.path.exists(script)
    backups = glob.glob(os.path.join(syncer.history_dir, "*", "Map0008.*"))
    assert sorted(os.path.basename(b) for b in backups) == ["Map0008.lmu", "Map0008.py"]
    assert "Map0008" not in syncer.state.data
    assert 8 not in MapTree.load(unit.bin_path).tree_order
    assert all(r.action == "none" and not r.message for r in _sync_all(syncer).values())


_END = "\n\n\nmaps = Maps()"


@pytest.mark.parametrize(
    "old, new, message",
    [
        (
            'dungeon_road = Map(id=3, name="Dungeon Road")',
            'dungeon_road = Map(id=3, name="Dungeon Road", parent_map=6)',
            "dungeon_road is in `with generate_dungeon:`, which is its parent: remove parent_map=",
        ),
        ("    with battle_test:\n", "    with picture_events:\n", "goes right after `picture_events = Map(...)`"),
        # a top-level parent_map= (a flat file of an older rpgsync): the same checks as the editor's
        (_END, '\n    lost = Map(name="Lost", parent_map=99)' + _END, "parent_map 99 is not in the map tree"),
        (_END, '\n    lost = Map(name="Lost", parent_map=9)' + _END, "is an area, which has no children"),
        ("dungeon_road = Map(id=3", "dungeon_road = Area(id=3", "a map cannot become an area"),
        ("party_map_id=1,", "party_map_id=77,", "start: party_map_id 77 is not a map of the tree"),
        ("boat_map_id=5,", "boat_map_id=9,", "start: boat_map_id 9 is not a map of the tree"),  # an area
        ("map_events = Map(\n        id=1,", "map_events = Map(\n        id=1, parent_map=3,", "its own ancestor"),
    ],
)
def test_impossible_trees_are_refused(exported2003, old, new, message):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    before = open(unit.bin_path, "rb").read()
    _edit(unit.py_path, old, new)
    r = syncer.sync(unit)
    assert r.action == "error" and message in r.message, r.message
    assert re.search(r"map_tree\.py:\d+: ", r.message)  # the line of the entry
    assert open(unit.bin_path, "rb").read() == before


def test_deleting_a_parent_only_or_every_map_is_refused(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    text = _text(unit.py_path)
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text.replace(_entry_line(text, "generate_dungeon"), ""))
    r = syncer.sync(unit)
    assert r.action == "error" and "with generate_dungeon: generate_dungeon is not a map of the tree" in r.message
    body = text.split("class Maps(MapTree):\n")[1].split("\n\nmaps = Maps()")[0]
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text.replace(body, "    pass\n"))
    assert "refusing to delete every map" in syncer.sync(unit).message
    assert all(os.path.exists(p) for p in syncer.project.map_paths().values())


def test_reorder(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    text = _text(unit.py_path)
    maze, road = _entry_line(text, "dungeon_maze"), _entry_line(text, "dungeon_road")
    text = text.replace(maze, "").replace(road, maze + road)
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text)
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("new tree order"), r.message
    order = MapTree.load(unit.bin_path).tree_order
    assert order[order.index(4) : order.index(4) + 5] == [4, 7, 3, 6, 8]


def test_vehicle_start_positions(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    _edit(unit.py_path, "boat_map_id=5,\n        boat_x=21,", "boat_map_id=4,\n        boat_x=5,")
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("new start positions"), r.message
    start = MapTree.load(unit.bin_path).start
    assert (start.get("boat_map_id"), start.get("boat_x"), start.get("ship_map_id")) == (4, 5, 5)
    text = _text(unit.py_path)
    assert "# New game: the party on Map events; boat on Generate Dungeon; ship, airship on Battle Test\n" in text
    assert all(r.action == "none" for r in _sync_all(syncer).values())
    # outside its map: written (like a teleport), reported by check at the line of start
    _edit(unit.py_path, "ship_x=25,", "ship_x=500,")
    assert syncer.sync(unit).action == "import"
    out = _cli("check", exported2003, "--no-types", ok=False)
    assert re.search(r"map_tree\.py:\d+: the ship starts on Battle Test \(map 5\) at \(500, 6\): outside the map", out)


def test_move_a_map(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    text = _text(unit.py_path)
    obstacle = _entry_line(text, "dungeon_obstacle")
    # out of the dungeons, under Map events (between Battle Test and Patches), with a note
    moved = "        # my own note\n" + obstacle[4:]
    text = text.replace(obstacle, "").replace("        patches = Map(", moved + "        patches = Map(")
    text = text.replace('name="Battle Test"', 'name="Arena"')
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text)
    r = syncer.sync(unit)
    assert r.action == "import" and "changed map 5, 8" in r.message and "new tree order" in r.message, r.message
    tree = MapTree.load(unit.bin_path)
    assert tree.info(8).get("parent_map") == 1 and tree.info(8)._chunk(0x03).raw == b"\x02"
    assert tree.tree_order.index(8) == tree.tree_order.index(10) - 1
    text = _text(unit.py_path)
    assert "# New game: the party on Map events; boat, ship, airship on Arena\n" in text  # follows the rename
    assert "        # my own note\n        dungeon_obstacle = " in text
    assert syncer.sync(unit).action == "none"
    # what a fresh export writes, but for the note
    assert text.replace("        # my own note\n", "") == unit.decompile(open(unit.bin_path, "rb").read())


def test_flat_file_of_an_older_rpgsync_is_written_nested(exported2003):
    from rpgsync.database import _flat
    from rpgsync.maptree import _entry_node, entries_from_bin, identifiers

    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    text = _text(unit.py_path)
    entries = entries_from_bin(MapTree.load(unit.bin_path), syncer.project.context())
    idents = identifiers({e.id: e.name for e in entries})
    start = re.search(r"    # New game.*?\n    start = Start\(.*?\n    \)\n", text, re.S).group(0)
    body = "".join("    %s = %s\n" % (idents[e.id], _flat(_entry_node(e, True))) for e in entries)
    flat = text.split("class Maps(MapTree):\n")[0] + "class Maps(MapTree):\n" + start + "\n" + body + _END + "\n"
    flat = flat.replace('name="Dungeon Road"', 'name="Old Road"')  # an edit not synced yet
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(flat)
    r = syncer.sync(unit)
    assert r.action == "import" and "changed map 3" in r.message, r.message
    assert MapTree.load(unit.bin_path).info(3).get("name") == b"Old Road"
    assert _text(unit.py_path) == text.replace('name="Dungeon Road"', 'name="Old Road"')
    assert syncer.sync(unit).action == "none"


def test_editor_changes(exported2003):
    """Renamed, added and deleted in the editor: map_tree.py follows, attribute names stay."""
    from rpgsync.lcf import ArrayItem
    from rpgsync.maptree import new_info

    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    _edit(unit.py_path, "    battle_test = Map(", "    arena = Map(")  # an attribute renamed by the user
    _edit(unit.py_path, "    with battle_test:", "    with arena:")
    assert syncer.sync(unit).action == "import"
    tree = MapTree.load(unit.bin_path)
    tree.info(5).set("name", b"Battle Arena")
    st = new_info("2k3", False)
    for key, value in (("name", b"Cave"), ("parent_map", 1), ("type", 1)):
        st.set(key, value)
    tree.maps.append(ArrayItem(id=30, struct=st))
    tree.tree_order.insert(2, 30)
    tree.maps = [it for it in tree.maps if it.id != 21]
    tree.tree_order.remove(21)
    with open(os.path.join(exported2003, "Map0030.lmu"), "wb") as f:
        f.write(blank_map())
    os.remove(os.path.join(exported2003, "Map0021.lmu"))
    with open(unit.bin_path, "wb") as f:
        f.write(tree.to_bytes())
    results = _sync_all(syncer)
    assert results["database/map_tree"].action == "export"
    text = _text(unit.py_path)
    assert '        arena = Map(id=5, name="Battle Arena")\n' in text
    assert '    with map_events:\n        cave = Map(id=30, name="Cave")\n' in text and "id=21" not in text
    assert results["Map0030"].action == "export"
    assert _text(syncer.project.script_for_map(5)).startswith('"""Map0005 - Battle Arena"""')
    assert syncer.project.context().handles["maps"][5] == "arena"
    # Map0021.py is left behind: reported, not deleted
    errors, warnings = tree_problems(str(exported2003), syncer.project.script_dir, "cp1252")
    assert errors == [] and warnings == ["Map0021.py: the game has no map 21 any more (deleted in the editor?)"]


def test_both_sides_merge(exported2003):
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    _edit(unit.py_path, 'name="Picture events"', 'name="Pictures"')
    tree = MapTree.load(unit.bin_path)
    tree.info(3).set("name", b"Road")
    with open(unit.bin_path, "wb") as f:
        f.write(tree.to_bytes())
    r = syncer.sync(unit)
    assert r.action == "merge", r.message
    tree = MapTree.load(unit.bin_path)
    assert (tree.info(2).get("name"), tree.info(3).get("name")) == (b"Pictures", b"Road")
    assert '            dungeon_road = Map(id=3, name="Road")\n' in _text(unit.py_path)


def test_browsing_the_tree_is_not_a_change(exported2003):
    """Scroll positions, expanded nodes and the selected map are the editor's."""
    syncer = _syncer(exported2003)
    unit = _tree_unit(syncer)
    tree = MapTree.load(unit.bin_path)
    tree.active_node = 7
    tree.info(4).set(0x07, b"\x00")
    with open(unit.bin_path, "wb") as f:
        f.write(tree.to_bytes())
    assert syncer.sync(unit).action == "none"


# -- maps.<name> in event scripts ------------------------------------------------------


def test_teleports_name_their_map(exported2003):
    syncer = _syncer(exported2003)
    text = _text(syncer.project.script_for_map(4))
    assert "teleport(maps.dungeon_road, 9, 2)" in text
    assert "from database.map_tree import maps\n" in text
    common = _text(syncer.project.common_events_script)
    assert "from database.map_tree import maps\n" in common


def test_unknown_map_name(exported2003):
    syncer = _syncer(exported2003)
    script = syncer.project.script_for_map(4)
    _edit(script, "teleport(maps.dungeon_road, 9, 2)", "teleport(maps.nowhere, 9, 2)")
    r = next(syncer.sync(u) for u in syncer.units() if u.name == "Map0004")
    assert r.action == "error" and "no map named 'nowhere' in database/map_tree.py" in r.message


# -- checks ------------------------------------------------------------------------------


class _Cmd:
    def __init__(self, code, params, line):
        self.code, self.params, self.line = code, params, line


class _Spec:
    def __init__(self, *commands):
        self.commands = list(commands)


def test_map_reference_problems():
    maps, sizes = {1: "Town", 2: "Cave"}, {1: (20, 15), 2: None}
    spec = _Spec(
        _Cmd(10810, [1, 3, 4, 0], 10),  # fine
        _Cmd(10810, [1, 20, 4, 0], 11),  # x outside
        _Cmd(10810, [7, 1, 1, 0], 12),  # no map 7
        _Cmd(10810, [2, 1, 1, 0], 13),  # no file
        _Cmd(10850, [1, 1, 5, 5, 5], 14),  # vehicle placed by variables: not checked
        _Cmd(10850, [1, 0, 0, 0, 0], 15),  # vehicle not placed
        _Cmd(11810, [1, 9, 0, 0, 0, 0], 16),  # remove_teleport_target: any map
        _Cmd(11810, [0, 9, 0, 0, 0, 0], 17),  # add_teleport_target
        _Cmd(11830, [1, 2, 99, 0, 0], 18),  # escape target y outside
    )
    assert map_reference_problems([spec], maps, sizes) == [
        (11, "teleport to Town (map 1) at (20, 4): outside the map (20x15)"),
        (12, "teleport to map 7, which is not in the map tree"),
        (13, "teleport to Cave (map 2): Map0002.lmu is missing"),
        (17, "teleport target to map 9, which is not in the map tree"),
        (18, "set_escape_target to Town (map 1) at (2, 99): outside the map (20x15)"),
    ]


def test_start_positions_are_places():
    maps, sizes = {1: "Town", 2: "Cave"}, {1: (20, 15), 2: (10, 10)}
    start = M.Start(party_map_id=1, party_x=3, party_y=4, boat_map_id=2, boat_x=10, boat_y=0, ship_map_id=7)
    assert map_reference_problems([StartSpec(start=start, lineno=5)], maps, sizes) == [
        (5, "the boat starts on Cave (map 2) at (10, 0): outside the map (10x10)"),
        (5, "the ship starts on map 7, which is not in the map tree"),
    ]


def _cli(*args, ok=True):
    r = CliRunner().invoke(cli.app, [str(a) for a in args])
    assert (r.exit_code == 0) == ok, r.output
    return r.output


def test_check_reports_map_references(exported2003):
    script = os.path.join(exported2003, "Scripts", "Map0004.py")
    _edit(script, "teleport(maps.dungeon_road, 9, 2)", "teleport(maps.dungeon_road, 90, 2)")
    _edit(script, "teleport(maps.dungeon_room, 9, 2)", "teleport(77, 9, 2)")
    open(os.path.join(exported2003, "Scripts", "Map0099.py"), "w").write("")
    out = _cli("check", exported2003, "--no-types", ok=False)
    assert re.search(r"Map0004\.py:\d+: teleport to Dungeon Road \(map 3\) at \(90, 2\): outside the map", out)
    assert re.search(r"Map0004\.py:\d+: teleport to map 77, which is not in the map tree", out)
    assert "Map0099.py: the game has no map 99 any more" in out
    out = _cli("status", exported2003)
    assert "Map tree" in out and "Map0099.py: the game has no map 99" in out


def test_locate_map_tree_entries(exported2003):
    path, line = _cli("locate", exported2003, "--map-tree", 3).strip().rsplit(":", 1)
    lines = _text(path).split("\n")
    assert path.endswith("map_tree.py") and lines[int(line) - 1].startswith("            dungeon_road = Map(id=3")
    path, line = _cli("locate", exported2003, "--map-tree", 0).strip().rsplit(":", 1)
    assert _text(path).split("\n")[int(line) - 1].startswith("    start = Start(")
    path, line = _cli("locate", exported2003, "--map", 4).strip().rsplit(":", 1)
    assert path.endswith("Map0004.py") and line == "1"
    assert "map 99 is not in" in _cli("locate", exported2003, "--map-tree", 99, ok=False)
