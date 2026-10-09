"""End-to-end behaviour of the sync engine on a copy of the 2003 test game."""

import os

from rpgsync.lcf import LcfFile
from rpgsync.project import Project
from rpgsync.sync import Syncer


def _setup(game):
    """Syncer and Map0001 unit of an exported copy of the test game (exported2003)."""
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    unit = next(u for u in syncer.units() if u.kind == "map" and u.map_id == 1)
    return syncer, unit


def _events(path):
    return {it.id: it.struct for it in LcfFile.load(path).root.get("events")}


def _edit(path, old, new):
    text = open(path, encoding="utf-8").read()
    assert old in text
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))


def test_export_creates_scripts_and_is_stable(game2003):
    syncer = Syncer(Project(str(game2003)), log=lambda *_: None)
    for u in syncer.units():
        assert syncer.sync(u).action == "export"
    unit = next(u for u in syncer.units() if u.kind == "map" and u.map_id == 1)
    assert os.path.exists(unit.py_path)
    assert all(syncer.sync(u).action == "none" for u in syncer.units())


def test_script_edit_patches_only_that_event(exported2003):
    syncer, unit = _setup(exported2003)
    before = open(unit.bin_path, "rb").read()
    _edit(unit.py_path, "variables.variable_0001 = 9999999", "variables[1] = 1234567")
    r = syncer.sync(unit)
    assert r.action == "import" and "changed event 9" in r.message
    after = LcfFile.load(unit.bin_path).root
    old = LcfFile.parse(before).root
    ev_after = {it.id: it.struct for it in after.get("events")}
    ev_old = {it.id: it.struct for it in old.get("events")}
    cmds = [c for pg in ev_after[9].get("pages") for c in pg.struct.get("event_commands")]
    assert any(c.code == 10220 and c.params[5] == 1234567 for c in cmds)
    # tiles and other events untouched
    assert after._chunk(0x47).raw == old._chunk(0x47).raw
    from rpgsync.lcf import write_struct

    for eid in ev_old:
        if eid != 9:
            assert write_struct(ev_after[eid]) == write_struct(ev_old[eid])
    assert syncer.sync(unit).action == "none"


def test_game_edit_regenerates_script(exported2003):
    syncer, unit = _setup(exported2003)
    f = LcfFile.load(unit.bin_path)
    ev = next(it for it in f.root.get("events") if it.id == 9)
    ev.struct.set("x", 11)
    f.save(unit.bin_path)
    r = syncer.sync(unit)
    assert r.action == "export"
    assert '@event(9, "EV0009", x=11, y=5)' in open(unit.py_path).read()


def test_both_changed_merges_per_event(exported2003):
    syncer, unit = _setup(exported2003)
    _edit(unit.py_path, "variables.variable_0001 = 9999999", "variables[1] = 42")
    f = LcfFile.load(unit.bin_path)
    next(it for it in f.root.get("events") if it.id == 1).struct.set("x", 0)
    f.save(unit.bin_path)
    r = syncer.sync(unit)
    assert r.action == "merge", r.message
    evs = _events(unit.bin_path)
    assert evs[1].get("x") == 0
    assert any(
        c.params[5:6] == [42] for pg in evs[9].get("pages") for c in pg.struct.get("event_commands") if c.code == 10220
    )
    text = open(unit.py_path).read()
    assert '@event(1, "EV0001", x=0, y=5)' in text and "variables.variable_0001 = 42" in text


def test_conflict_keeps_game_version_and_saves_script(exported2003):
    syncer, unit = _setup(exported2003)
    _edit(unit.py_path, '@event(9, "EV0009", x=10, y=5)', '@event(9, "EV0009", x=8, y=5)')
    f = LcfFile.load(unit.bin_path)
    next(it for it in f.root.get("events") if it.id == 9).struct.set("x", 11)
    f.save(unit.bin_path)
    r = syncer.sync(unit)
    assert r.action == "conflict"
    assert _events(unit.bin_path)[9].get("x") == 11
    conflicts = [p for p in os.listdir(os.path.dirname(unit.py_path)) if ".conflict-" in p]
    assert conflicts and "x=8" in open(os.path.join(os.path.dirname(unit.py_path), conflicts[0])).read()


def test_new_event_gets_id_written_back(exported2003):
    syncer, unit = _setup(exported2003)
    before = set(_events(unit.bin_path))
    with open(unit.py_path, "a", encoding="utf-8") as f:
        f.write(
            '\n\n@event(name="Guide", x=8, y=6)\ndef guide():\n'
            '    @page(sprite=("Actor1", 0))\n    def page_1():\n        text("Hello!")\n'
        )
    r = syncer.sync(unit)
    assert r.action == "import", r.message
    evs = _events(unit.bin_path)
    new_id = min(set(range(1, max(before) + 2)) - before)  # lowest free id, like the editor
    assert evs[new_id].get("name") == b"Guide"
    assert ("@event(%d, name=" % new_id) in open(unit.py_path).read()
    assert syncer.sync(unit).action == "none"


def test_compile_error_reports_line_and_keeps_game_file(exported2003):
    syncer, unit = _setup(exported2003)
    before = open(unit.bin_path, "rb").read()
    _edit(unit.py_path, "variables.variable_0001 = 9999999", 'variables[1] =+ "x"')
    r = syncer.sync(unit)
    assert r.action == "error" and "Map0001.py:" in r.message and "unsupported value" in r.message
    assert open(unit.bin_path, "rb").read() == before


def test_common_events_keep_database_positions(exported2003):
    """Database entries are looked up by position: never leave holes."""
    from rpgsync.sync import CommonEventsUnit

    syncer, _ = _setup(exported2003)
    unit = CommonEventsUnit(syncer.project)
    text = open(unit.py_path, encoding="utf-8").read()
    assert '"", trigger="call")' not in text  # empty slots are not listed
    first = text.index("    @common_event(")
    second = text.index("\n    @common_event(", first + 1)
    removed_id = int(text[first + len("    @common_event(") :].split(",")[0])
    text = text[:first] + text[second + 1 :]  # delete the first common event
    new = (
        '    @common_event(name="New")\n    def new():\n        text("hi")\n\n'
        '    @common_event(999, "Far")\n    def far():\n        text("far")\n\n\n'
    )
    end = text.index("\n\n\ncommon_events = CommonEvents()")
    text = text[:end] + "\n\n" + new.rstrip("\n") + text[end:]
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text)
    r = syncer.sync(unit)
    assert r.action == "import", r.message
    ces = LcfFile.load(unit.bin_path).root.get("commonevents")
    assert [it.id for it in ces] == list(range(1, len(ces) + 1))
    assert len(ces) == 999 and ces[998].struct.get("name") == b"Far"
    assert ces[removed_id - 1].struct.get("name") == b""  # deleted -> emptied in place
    assert ces[removed_id - 1].struct.get("event_commands") == []
    assert sum(it.struct.get("name") == b"New" for it in ces) == 1
    assert syncer.sync(unit).action == "none"


def test_database_table_syncs_both_ways(exported2003):
    from rpgsync.sync import DatabaseUnit

    syncer, _ = _setup(exported2003)
    unit = next(u for u in syncer.units() if isinstance(u, DatabaseUnit) and u.table == "enemies")
    text = open(unit.py_path, encoding="utf-8").read()
    import re

    m = re.search(r"max_hp=(\d+)", text)
    old_hp = int(m.group(1))
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text[: m.start()] + "max_hp=%d" % (old_hp + 1) + text[m.end() :])
    r = syncer.sync(unit)
    assert r.action == "import" and "changed entry" in r.message, r.message
    enemies = LcfFile.load(unit.bin_path).root.get("enemies")
    assert any(it.struct.get("max_hp") == old_hp + 1 for it in enemies)
    # other units sharing RPG_RT.ldb only see an unchanged regeneration
    assert all(syncer.sync(u).action in ("none", "export") for u in syncer.units())
    assert all(syncer.sync(u).action == "none" for u in syncer.units())


def test_engine_problems_block_the_write(exported2003):
    syncer, unit = _setup(exported2003)
    before = open(unit.bin_path, "rb").read()
    line = next(ln for ln in open(unit.py_path).read().splitlines() if "variables.variable_0001 = 9999999" in ln)
    indent = line[: len(line) - len(line.lstrip())]
    _edit(unit.py_path, line, line + "\n" + indent + "cmd(3001, 0)")
    r = syncer.sync(unit)
    assert r.action == "error" and "unknown command 3001" in r.message and "maniac" in r.message
    assert open(unit.bin_path, "rb").read() == before
    # declaring the patch in the scripts' pyproject.toml lets it through
    with open(os.path.join(syncer.project.script_dir, "pyproject.toml"), "w") as f:
        f.write('[tool.rpgsync]\npatches = ["maniac"]\n')
    assert syncer.sync(unit, "script").action == "import"


def _db_setup(game):
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    return syncer, {u.name: u for u in syncer.units()}


def test_database_entries_need_no_order_nor_id(exported2003):
    syncer, units = _db_setup(exported2003)
    items = units["database/items"]
    slots = len(LcfFile.load(items.bin_path).root.get("items"))
    # new entry without id, written first: it goes after the last slot and keeps its attribute name
    _edit(items.py_path, "size = %d" % slots, 'size = %d\n    rusty_key = Item(name="Nouveau", price=7)' % slots)
    r = syncer.sync(items)
    assert r.action == "import" and "added entry %d" % (slots + 1) in r.message, r.message
    text = open(items.py_path, encoding="utf-8").read()
    assert 'rusty_key = Item(id=%d, name="Nouveau", price=7' % (slots + 1) in text
    assert "size = %d" % (slots + 1) in text
    assert syncer.sync(items).action == "none"
    # removing it again leaves an empty slot, like the editor; lowering size drops it
    text = open(items.py_path, encoding="utf-8").read()
    start = text.index("    rusty_key = ")
    with open(items.py_path, "w", encoding="utf-8") as f:
        f.write(text[:start] + text[text.index("\n", start) + 1 :])
    r = syncer.sync(items)
    assert "removed entry %d" % (slots + 1) in r.message
    assert len(LcfFile.load(items.bin_path).root.get("items")) == slots + 1
    _edit(items.py_path, "size = %d" % (slots + 1), "size = %d" % slots)
    assert "size %d -> %d" % (slots + 1, slots) in syncer.sync(items).message
    assert len(LcfFile.load(items.bin_path).root.get("items")) == slots
    # size cannot cut an entry that is still listed
    _edit(items.py_path, "size = %d" % slots, "size = 1")
    r = syncer.sync(items)
    assert r.action == "error" and "would delete entries" in r.message


def test_database_tables_only_see_their_own_changes(exported2003):
    syncer, units = _db_setup(exported2003)
    _edit(units["database/items"].py_path, "price=", "price=1")
    assert syncer.sync(units["database/items"]).action == "import"
    for name in ("database/enemies", "CommonEvents"):
        assert syncer.sync(units[name]).action == "none"


def test_dynrpg_and_engine_declared_in_pyproject(exported2003):
    syncer, unit = _setup(exported2003)
    line = next(ln for ln in open(unit.py_path).read().splitlines() if "variables.variable_0001 = 9999999" in ln)
    indent = line[: len(line) - len(line.lstrip())]
    _edit(unit.py_path, line, line + "\n" + indent + 'dyn.write_text("id", 10, 20, "Hello")')
    r = syncer.sync(unit)
    assert r.action == "error" and 'add "dynrpg"' in r.message
    pyproject = os.path.join(syncer.project.script_dir, "pyproject.toml")
    with open(pyproject, "w") as f:
        f.write('[tool.rpgsync]\npatches = ["dynrpg"]\n')
    assert syncer.sync(unit, "script").action == "import"
    # a 2003 game can be held to RPG Maker 2000
    _edit(unit.py_path, line, line + "\n" + indent + "wait(1.0, key=True)")
    with open(pyproject, "w") as f:
        f.write('[tool.rpgsync]\nengine = "2000"\npatches = ["dynrpg"]\n')
    r = syncer.sync(unit)
    assert r.action == "error" and "RPG Maker 2003 parameters" in r.message, r.message


def test_map_events_by_name(exported2003):
    import re

    syncer, unit = _setup(exported2003)
    text = open(unit.py_path, encoding="utf-8").read()
    name, eid = next(
        (n, int(i)) for i, n in re.findall(r"@event\((\d+), .*\)\ndef (\w+)\(", text) if not n.startswith("ev_")
    )
    line = next(ln for ln in text.splitlines() if "variables.variable_0001 = 9999999" in ln)
    indent = line[: len(line) - len(line.lstrip())]
    _edit(unit.py_path, line, line + "\n" + indent + "%s.show_animation(1)" % name)
    assert syncer.sync(unit).action == "import"
    cmds = [c for pg in _events(unit.bin_path)[9].get("pages") for c in pg.struct.get("event_commands")]
    assert any(c.code == 11210 and c.params[1] == eid for c in cmds)
    # the regenerated script refers to it by name too
    syncer.sync(unit, "game")
    assert "%s.show_animation(1)" % name in open(unit.py_path, encoding="utf-8").read()
    # an event without id cannot be referred to yet
    _edit(unit.py_path, "%s.show_animation(1)" % name, "newcomer.show_animation(1)")
    with open(unit.py_path, "a", encoding="utf-8") as f:
        f.write('\n\n@event(name="Newcomer", x=1, y=1)\ndef newcomer():\n    pass\n')
    r = syncer.sync(unit)
    assert r.action == "error" and "newcomer has no id yet" in r.message


def test_variable_names_belong_to_the_scripts(exported2003):
    import re

    syncer, unit = _setup(exported2003)
    variables = next(u for u in syncer.units() if u.name == "database/variables")
    text = open(variables.py_path, encoding="utf-8").read()
    attr = re.search(r"^    (\w+) = Variable\(1, ", text, re.M).group(1)
    assert "variables.%s" % attr in open(unit.py_path, encoding="utf-8").read()
    before = open(unit.py_path, encoding="utf-8").read()
    # renamed in the editor: the attribute name stays, the scripts do not change
    f = LcfFile.load(syncer.project.ldb_path)
    next(it for it in f.root.get("variables") if it.id == 1).struct.set("name", b"Big Counter")
    f.save(syncer.project.ldb_path)
    for u in syncer.units():
        syncer.sync(u)
    assert '%s = Variable(1, "Big Counter")' % attr in open(variables.py_path, encoding="utf-8").read()
    assert open(unit.py_path, encoding="utf-8").read() == before
    # renamed in the scripts (an IDE rename): nothing changes in the game
    game = open(unit.bin_path, "rb").read(), open(syncer.project.ldb_path, "rb").read()
    for path in (variables.py_path, unit.py_path):  # like an IDE rename: every occurrence
        with open(path, encoding="utf-8") as f:
            renamed = re.sub(r"\b%s\b" % attr, "big_counter", f.read())
        with open(path, "w", encoding="utf-8") as f:
            f.write(renamed)
    for u in (variables, unit):
        r = syncer.sync(u)
        assert r.action == "import" and r.message.endswith("no changes"), r.message
    assert (open(unit.bin_path, "rb").read(), open(syncer.project.ldb_path, "rb").read()) == game
    assert "variables.big_counter" in open(unit.py_path, encoding="utf-8").read()


def test_common_events_by_name(exported2003):
    import re

    syncer, unit = _setup(exported2003)
    ce_text = open(syncer.project.common_events_script, encoding="utf-8").read()
    assert "class CommonEvents(CommonEventTable):" in ce_text and "common_events = CommonEvents()" in ce_text
    cid, fname = next(
        (int(i), f)
        for i, f in re.findall(r"@common_event\((\d+), .*\)\n    def (\w+)\(", ce_text)
        if not f.startswith("ce_")
    )
    line = next(ln for ln in open(unit.py_path).read().splitlines() if "variables.variable_0001 = 9999999" in ln)
    indent = line[: len(line) - len(line.lstrip())]
    _edit(unit.py_path, line, "%s\n%scommon_events.%s()\n%scommon_events[%d]()" % (line, indent, fname, indent, cid))
    assert syncer.sync(unit).action == "import"
    cmds = [c for pg in _events(unit.bin_path)[9].get("pages") for c in pg.struct.get("event_commands")]
    assert sum(c.code == 12330 and list(c.params) == [0, cid, 0] for c in cmds) == 2
    syncer.sync(unit, "game")  # regenerated: both are written by name
    assert open(unit.py_path, encoding="utf-8").read().count("common_events.%s()" % fname) >= 2
    _edit(unit.py_path, "common_events.%s()" % fname, "common_events.no_such_event()")
    r = syncer.sync(unit)
    assert r.action == "error" and "no common event named 'no_such_event'" in r.message


def test_move_route_switch_steps_by_name(exported2003):
    import re

    syncer, unit = _setup(exported2003)
    switches = open(os.path.join(syncer.project.script_dir, "database", "switches.py"), encoding="utf-8").read()
    sid, attr = next((int(i), a) for a, i in re.findall(r"^    (\w+) = Switch\((\d+), ", switches, re.M))
    line = next(ln for ln in open(unit.py_path).read().splitlines() if "variables.variable_0001 = 9999999" in ln)
    indent = line[: len(line) - len(line.lstrip())]
    step = "this.move(switch_on(switches.%s), switch_off(switches[%d]), switch_on(%d))" % (attr, sid, sid)
    _edit(unit.py_path, line, line + "\n" + indent + step)
    assert syncer.sync(unit).action == "import"
    cmds = [c for pg in _events(unit.bin_path)[9].get("pages") for c in pg.struct.get("event_commands")]
    from rpgsync.lcf import MoveCommand, write_move_commands

    moves = [MoveCommand(code=32, params=[sid]), MoveCommand(code=33, params=[sid]), MoveCommand(code=32, params=[sid])]
    assert any(c.code == 11330 and list(c.params[4:]) == list(write_move_commands(moves)) for c in cmds)
    syncer.sync(unit, "game")  # regenerated: by name
    expected = "this.move(switch_on(switches.%s),switch_off(switches.%s),switch_on(switches.%s)," % ((attr,) * 3)
    text = re.sub(r"\s+|,(?=\s*\))", "", open(unit.py_path, encoding="utf-8").read())  # ruff may split the line
    assert expected in text
