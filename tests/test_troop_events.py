"""Troop battle events <-> database/troop_events.py: conditions, battle commands,
incremental sync, and the troops shared with database/troops.py."""

import os
import shutil

import pytest
from conftest import TESTGAME

from rpgsync import script as S
from rpgsync.lcf import Command, CommandList, LcfFile, write_array, write_struct
from rpgsync.project import Project
from rpgsync.sync import Syncer, read_bytes

# a troop of the 2003 test game without battle events, given some below
ALL_CONDITIONS = """\
    @troop(1, "Slime*2")
    def slimes():
        @page(
            when=switches[1]
            and switches[2]
            and variables[3] >= 7
            and turn(2, every=3)
            and fatigue(10, 60)
            and enemies[1].hp_percent(5, 50)
            and actors[2].hp_percent(0, 30)
        )
        def page_1():
            text("Every RPG Maker 2000 condition")

        @page(when=enemies[0].turn(1, every=2) and actors[1].turn(3) and actors[4].uses_command(2))
        def page_2():
            if battle_condition(switches[5]):
                enemies[0].change_hp(-10, lethal=True)
            elif battle_condition(variables[3] >= 10):
                enemies[1].change_sp(5)
            else:
                enemies[2].add_state(3)
            if battle_condition(actors[1].can_act):
                enemies[0].show()
            if battle_condition(enemies[1].can_act):
                set_battle_background("Castle")
            if battle_condition(enemies[2].targeted):
                show_battle_animation(3, 0, wait=True)
            if battle_condition(actors[2].uses_command(4)):
                force_flee("party")
            if battle_condition(cond(6, 1, 2, 3, 4)):
                enable_combo(1, 2, 3)
            battle_call_common_event(1)
            end_battle()

        @page(switch_b=12)
        def page_3():
            pass

        @page(id=7, when=turn(0))
        def page_4():
            enemies[0].remove_state(2)

        @page(raw=True)
        def page_5():
            cmd("ShowMessage", text="no block holds this indent", indent=1)

"""


def _setup(game):
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    unit = next(u for u in syncer.units() if u.kind == "troop")
    return syncer, unit


def _edit(path, old, new):
    text = open(path, encoding="utf-8").read()
    assert old in text, old
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))


def _troops(data: bytes) -> dict:
    return {it.id: it.struct for it in LcfFile.parse(data).root.get("troops")}


def _pages(data: bytes, tid: int) -> list:
    return S.troop_pages(_troops(data)[tid])


def _only_pages_of(before: bytes, after: bytes, tids: set[int]) -> None:
    """`after` is `before` but for the `pages` chunk of these troops."""
    a, b = LcfFile.parse(before).root, LcfFile.parse(after).root
    assert [(c.id, c.raw) for c in a.chunks if c.id != 0x0F] == [(c.id, c.raw) for c in b.chunks if c.id != 0x0F]
    ta, tb = a.get("troops"), b.get("troops")
    assert [t.id for t in ta] == [t.id for t in tb]
    for x, y in zip(ta, tb):
        if x.id in tids:
            assert [(c.id, c.raw) for c in x.struct.chunks if c.id != 0x0B] == [
                (c.id, c.raw) for c in y.struct.chunks if c.id != 0x0B
            ]
            assert x.struct.get("pages", None) != y.struct.get("pages", None), x.id
        else:
            assert write_struct(x.struct) == write_struct(y.struct), x.id


def _add_slimes(unit):
    _edit(unit.py_path, '    @troop(89, "Test Battle")', ALL_CONDITIONS + '    @troop(89, "Test Battle")')


def test_export_lists_troops_with_battle_events(exported2003):
    syncer, unit = _setup(exported2003)
    text = open(unit.py_path, encoding="utf-8").read()
    assert "class TroopEvents(TroopEventTable):" in text and text.endswith("troop_events = TroopEvents()\n")
    assert [line for line in text.split("\n") if line.startswith("    @troop(")] == ['    @troop(89, "Test Battle")']
    assert "        @page(when=switches.test_battle_on)\n        def page_1():" in text
    assert "        @page(switch_b=13)\n        def page_2():" in text  # the editor's 2nd switch slot alone
    assert "enemies[0].change_hp(-9999)" in text and "raw=True" not in text
    assert all(syncer.sync(u).action == "none" for u in syncer.units())


def test_every_condition_and_battle_command_compiles_back(exported2003):
    syncer, unit = _setup(exported2003)
    before = read_bytes(unit.bin_path)
    _add_slimes(unit)
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("added troop 1"), r.message
    # the name in @troop(...) is a label: it shows the troop's name, set in troops.py
    assert '    @troop(1, "Plains 1")\n    def slimes():' in open(unit.py_path, encoding="utf-8").read()
    after = read_bytes(unit.bin_path)
    _only_pages_of(before, after, {1})

    pages = _pages(after, 1)
    assert [p.id for p in pages] == [1, 2, 3, 7, 5]
    c1, c2, c3 = (pages[n].struct.get("condition") for n in range(3))
    assert list(c1.get("flags")) == [0x7F, 0]
    assert [c1.get(f) for f in ("switch_a_id", "switch_b_id", "variable_id", "variable_value")] == [1, 2, 3, 7]
    assert [c1.get(f) for f in ("turn_a", "turn_b", "fatigue_min", "fatigue_max")] == [2, 3, 10, 60]
    assert [c1.get(f) for f in ("enemy_id", "enemy_hp_min", "enemy_hp_max")] == [1, 5, 50]
    assert [c1.get(f) for f in ("actor_id", "actor_hp_min", "actor_hp_max")] == [2, 0, 30]
    assert list(c2.get("flags")) == [0x80, 0x03]
    assert [c2.get(f) for f in ("turn_enemy_id", "turn_enemy_a", "turn_enemy_b")] == [0, 1, 2]
    assert [c2.get(f) for f in ("turn_actor_id", "turn_actor_a", "turn_actor_b")] == [1, 3, 0]
    assert [c2.get(f) for f in ("command_actor_id", "command_id")] == [4, 2]
    assert list(c3.get("flags")) == [2, 0] and c3.get("switch_b_id") == 12
    codes = [c.code for c in pages[1].struct.get("event_commands")]
    for code in (13310, 23310, 13110, 13120, 13130, 13150, 13210, 13260, 1006, 1007, 1005, 13410):
        assert code in codes, code
    # no default value is written; a new page is laid out like the editor's
    assert [c.id for c in c3.chunks] == [0x01, 0x03]
    assert [c.id for c in pages[2].struct.chunks] == [0x02, 0x0B, 0x0C]

    # what the game now holds decompiles to the same entry, and compiles back exactly
    regenerated = unit.decompile(after)
    assert "@troop(1, " in regenerated
    entry = regenerated[regenerated.index("@troop(1, ") : regenerated.index("@troop(89, ")]
    assert "when=switches[1]\n" in entry and "and turn(2, every=3)\n" in entry
    assert "@page(when=enemies[0].turn(1, every=2) and actors[1].turn(3) and actors[4].uses_command(2))" in entry
    assert (
        "elif battle_condition(variables.variable_0003 >= 10):" in entry
        and "if battle_condition(cond(6, 1, 2, 3, 4)):" in entry
    )
    assert "@page(switch_b=12)" in entry and "@page(id=7, when=turn(0))" in entry and "@page(raw=True)" in entry
    assert [s.key() for s in unit.compile(regenerated)] == [s.key() for s in unit.specs_from_bin(after)]
    assert unit.apply(after, unit.compile(regenerated)) == (after, {"added": [], "removed": [], "changed": []})
    assert syncer.sync(unit).action == "none"


def test_edit_one_page_patches_only_that_troop(exported2003):
    syncer, unit = _setup(exported2003)
    _add_slimes(unit)
    syncer.sync(unit)
    before = read_bytes(unit.bin_path)
    pages_before = _pages(before, 89)
    calls = {"incremental": 0}
    incremental = unit.compile_incremental

    def spy(text, base):
        specs = incremental(text, base)
        calls["incremental"] += specs is not None
        return specs

    unit.compile_incremental = spy
    _edit(unit.py_path, 'text("This is a test battle.")', 'text("This is a battle test.")')
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("changed troop 89"), r.message
    assert calls["incremental"] == 1  # troop 1 is not even compiled
    after = read_bytes(unit.bin_path)
    _only_pages_of(before, after, {89})
    pages = _pages(after, 89)
    assert write_struct(pages[1].struct) == write_struct(pages_before[1].struct)  # the other page
    assert pages[0].struct.get("condition") is not None
    assert pages[0].struct.get("event_commands")[0].string == b"This is a battle test."
    # every other unit (troops.py too) sees nothing new
    assert all(syncer.sync(u).action == "none" for u in syncer.units())


def test_game_edit_regenerates_only_that_troop(exported2003):
    syncer, unit = _setup(exported2003)
    _add_slimes(unit)
    syncer.sync(unit)
    syncer.sync(unit, "game")  # the script as rpgsync writes it
    f = LcfFile.load(unit.bin_path)
    troop = next(it for it in f.root.get("troops") if it.id == 89)
    pages = S.troop_pages(troop.struct)
    p = pages[0].struct
    p.set("event_commands", CommandList([Command(11410, 0, b"", [7])] + list(p.get("event_commands"))))
    troop.struct.set("pages", write_array(pages))
    with open(unit.bin_path, "wb") as out:
        out.write(f.to_bytes())

    whole = unit.decompile
    calls = []
    unit.decompile = lambda d: calls.append(d) or whole(d)
    r = syncer.sync(unit)
    unit.decompile = whole
    assert r.action == "export" and not calls  # only troop 89 was regenerated
    text = open(unit.py_path, encoding="utf-8").read()
    assert text == whole(read_bytes(unit.bin_path))
    assert 'def page_1():\n            cmd("Wait", 7)\n' in text


def test_conflict_keeps_the_game_version(exported2003):
    syncer, unit = _setup(exported2003)
    _edit(unit.py_path, 'text("This is a test battle.")', 'text("Mine.")')
    f = LcfFile.load(unit.bin_path)
    troop = next(it for it in f.root.get("troops") if it.id == 89)
    pages = S.troop_pages(troop.struct)
    p = pages[0].struct
    cmds = list(p.get("event_commands"))
    cmds[0] = Command(10110, 0, b"Theirs.")
    p.set("event_commands", CommandList(cmds))
    troop.struct.set("pages", write_array(pages))
    f.save(unit.bin_path)
    r = syncer.sync(unit)
    assert r.action == "conflict" and "CONFLICT on troop 89" in r.message, r.message
    assert 'text("Theirs.")' in open(unit.py_path, encoding="utf-8").read()
    saved = [n for n in os.listdir(os.path.dirname(unit.py_path)) if n.startswith("troop_events.conflict-")]
    assert len(saved) == 1
    assert 'text("Mine.")' in open(os.path.join(os.path.dirname(unit.py_path), saved[0]), encoding="utf-8").read()
    assert _pages(read_bytes(unit.bin_path), 89)[0].struct.get("event_commands")[0].string == b"Theirs."


def test_troops_and_troop_events_share_the_troops(exported2003):
    """troops.py and troop_events.py edit the same Troop entries, each its own part."""
    syncer, unit = _setup(exported2003)
    troops = next(u for u in syncer.units() if u.name == "database/troops")
    pages = read_bytes(unit.bin_path)
    pages = _troops(pages)[89].get("pages")
    # both scripts edited at once: both edits reach the game
    _edit(troops.py_path, 'name="Test Battle",', 'name="Battle Test",')
    _edit(unit.py_path, 'text("This is a test battle.")', 'text("Both.")')
    results = {u.name: syncer.sync(u) for u in syncer.units()}
    assert results["database/troops"].action == "import"
    # the name in troop_events.py is a label, now out of date: its script is merged
    assert results["TroopEvents"].action == "merge", results["TroopEvents"].message
    troop = _troops(read_bytes(unit.bin_path))[89]
    assert troop.get("name") == b"Battle Test"
    assert S.troop_pages(troop)[0].struct.get("event_commands")[0].string == b"Both."
    text = open(unit.py_path, encoding="utf-8").read()
    assert '@troop(89, "Battle Test")' in text and 'text("Both.")' in text
    assert all(syncer.sync(u).action == "none" for u in syncer.units())
    # a name changed in troop_events.py is put back: troops.py names the troops
    before = read_bytes(unit.bin_path)
    _edit(unit.py_path, '@troop(89, "Battle Test")', '@troop(89, "Renamed")')
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("no changes"), r.message
    assert '@troop(89, "Battle Test")' in open(unit.py_path, encoding="utf-8").read()
    assert read_bytes(unit.bin_path) == before

    # removing the troop from troops.py keeps its battle events (troop_events.py has them)
    pages = _troops(read_bytes(unit.bin_path))[89].get("pages")
    text = open(troops.py_path, encoding="utf-8").read()
    start = text.index("    test_battle = Troop(")
    end = text.index("\n    )\n", start) + len("\n    )\n")
    with open(troops.py_path, "w", encoding="utf-8") as f:
        f.write(text[:start] + text[end:])
    r = syncer.sync(troops)
    assert r.action == "import" and "removed entry 89" in r.message, r.message
    troop = _troops(read_bytes(unit.bin_path))[89]
    assert troop.get("name") == b"" and troop.get("pages") == pages
    assert syncer.sync(unit).action == "export"  # only the label changed
    assert '@troop(89, "")' in open(unit.py_path, encoding="utf-8").read()


def test_removed_entry_gets_one_empty_page(exported2003):
    syncer, unit = _setup(exported2003)
    before = read_bytes(unit.bin_path)
    text = open(unit.py_path, encoding="utf-8").read()
    start = text.index('    @troop(89, "Test Battle")')
    end = text.index("\n\ntroop_events = ")
    with open(unit.py_path, "w", encoding="utf-8") as f:
        f.write(text[:start] + "    pass" + text[end:])
    r = syncer.sync(unit)
    assert r.action == "import" and "removed troop 89 (back to one empty page)" in r.message, r.message
    after = read_bytes(unit.bin_path)
    _only_pages_of(before, after, {89})
    # exactly what the editor holds for a troop without battle events
    assert _troops(after)[89].get("pages") == _troops(after)[88].get("pages")
    assert "@troop(" not in open(unit.py_path, encoding="utf-8").read()


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('@troop(89, "Test Battle")', '@troop(999, "Test Battle")', "troop 999 is not in the database"),
        ("@page(switch_b=13)", '@page(switch_b=13, trigger="action")', "a troop page() takes when="),
        ("@page(switch_b=13)", "@page(when=variables[1] == 3)", "troop pages only test variables[id] >= value"),
        ("@page(switch_b=13)", "@page(when=party.gold > 3)", "unsupported troop page condition"),
        ("@page(switch_b=13)", "@page(when=turn(1) and turn(2))", "only have one turn condition"),
        ('@troop(89, "Test Battle")', '@troop("Test Battle")', "needs the troop id"),
    ],
)
def test_script_errors(exported2003, old, new, message):
    syncer, unit = _setup(exported2003)
    before = read_bytes(unit.bin_path)
    _edit(unit.py_path, old, new)
    r = syncer.sync(unit)
    assert r.action == "error" and message in r.message, r.message
    assert read_bytes(unit.bin_path) == before


def test_rpg_maker_2000_conditions(tmp_path):
    src = os.path.join(TESTGAME, "TestGame-2000")
    if not os.path.isdir(src):
        pytest.skip("TestGame-2000 not found")
    game = tmp_path / "game"
    shutil.copytree(src, game, ignore=shutil.ignore_patterns("Scripts"))
    syncer, unit = _setup(game)
    for u in syncer.units():
        if u.kind != "map":
            assert syncer.sync(u).action == "export"
    text = open(unit.py_path, encoding="utf-8").read()
    assert '    @troop(1, "Slimex2")\n    def slimex2():\n        @page(when=turn(0))\n' in text
    before = read_bytes(unit.bin_path)
    _edit(unit.py_path, "@page(when=turn(0))", "@page(when=enemies[0].turn(1))")
    r = syncer.sync(unit)
    assert r.action == "error" and "turn_enemy only exists in RPG Maker 2003" in r.message, r.message
    _edit(unit.py_path, "@page(when=enemies[0].turn(1))", "@page(when=turn(0) and enemies[1].hp_percent(0, 20))")
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("changed troop 1"), r.message
    _only_pages_of(before, read_bytes(unit.bin_path), {1})
    cond = _pages(read_bytes(unit.bin_path), 1)[0].struct.get("condition")
    assert list(cond.get("flags")) == [8 | 32]  # one byte in 2000


def test_watch_syncs_troop_events(exported2003, monkeypatch):
    """The watch loop syncs troop_events.py like the other scripts."""
    from rpgsync import sync

    syncer, unit = _setup(exported2003)
    lines = []
    syncer.log = lines.append
    _edit(unit.py_path, 'text("This is a test battle.")', 'text("Watched.")')
    passes = []

    def sleep(_):
        passes.append(1)
        if len(passes) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(sync.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        syncer.watch(interval=0, announce=False)
    assert any("TroopEvents: troop_events.py -> RPG_RT.ldb: changed troop 89" in line for line in lines), lines
    assert _pages(read_bytes(unit.bin_path), 89)[0].struct.get("event_commands")[0].string == b"Watched."


def test_troops_synced_by_an_older_rpgsync_stay_synced(exported2003):
    """Before troop_events.py, troops.py was synced against the whole troops chunk."""
    from rpgsync.sync import _chunk_sha

    syncer, unit = _setup(exported2003)
    troops = next(u for u in syncer.units() if u.name == "database/troops")
    data = read_bytes(troops.bin_path)
    syncer.state.data[troops.name]["bin"] = _chunk_sha(data, "troops")
    syncer.state.save()
    assert syncer.sync(troops).action == "none"
    assert syncer.state.get(troops)["bin"] == troops.fingerprint(data)  # recorded in the new form
    # so that a later troop_events.py push is not seen as a change of troops.py
    _edit(unit.py_path, 'text("This is a test battle.")', 'text("Later.")')
    assert syncer.sync(unit).action == "import"
    assert syncer.sync(troops).action == "none"
