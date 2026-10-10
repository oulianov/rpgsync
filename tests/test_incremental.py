"""Incremental compile: after an edit, only the entries whose source changed are
compiled; the game's other entries are left untouched.  The result must be the
very same as compiling everything."""

import pytest

from rpgsync import script as S
from rpgsync.project import Project
from rpgsync.sync import Syncer, read_bytes


def _setup(game):
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    unit = next(u for u in syncer.units() if u.kind == "common")
    calls = {"incremental": 0, "full": 0}
    incremental, full = unit.compile_incremental, unit.compile

    def spy_incremental(text, base):
        specs = incremental(text, base)
        calls["incremental"] += specs is not None
        return specs

    def spy_full(text):
        calls["full"] += 1
        return full(text)

    unit.compile_incremental, unit.compile = spy_incremental, spy_full
    return syncer, unit, calls, full


def _edit(path, old, new):
    text = open(path, encoding="utf-8").read()
    assert old in text, old
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))


def _same_as_full_compile(unit, full, before: bytes) -> bool:
    specs = full(open(unit.py_path, encoding="utf-8").read())
    return read_bytes(unit.bin_path) == unit.apply(before, specs)[0]


def test_edit_compiles_only_that_common_event(exported2003):
    syncer, unit, calls, full = _setup(exported2003)
    before = read_bytes(unit.bin_path)
    _edit(unit.py_path, 'text("Change each system BGM.")', 'text("Change every system BGM.")')
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("changed common event 5")
    assert calls == {"incremental": 1, "full": 1}  # the one compile: the edited event only
    assert _same_as_full_compile(unit, full, before)
    assert syncer.sync(unit).action == "none"


def test_two_edits_in_a_row(exported2003):
    syncer, unit, calls, full = _setup(exported2003)
    _edit(unit.py_path, 'text("Change each system BGM.")', 'text("Change every system BGM.")')
    syncer.sync(unit)
    before = read_bytes(unit.bin_path)
    _edit(unit.py_path, "variables.variable_0001 = 2", "variables.variable_0001 = 3")
    r = syncer.sync(unit)
    assert r.message.endswith("changed common event 1") and calls["incremental"] == 2
    assert _same_as_full_compile(unit, full, before)


def test_change_outside_the_events_compiles_everything(exported2003):
    syncer, unit, calls, _ = _setup(exported2003)
    _edit(unit.py_path, "class CommonEvents(", "# a comment between the imports and the class\nclass CommonEvents(")
    _edit(unit.py_path, 'text("Change each system BGM.")', 'text("Change every system BGM.")')
    r = syncer.sync(unit)
    assert r.message.endswith("changed common event 5")
    assert calls == {"incremental": 0, "full": 1}


def test_error_in_an_edited_event_has_the_right_line(exported2003):
    syncer, unit, _, _ = _setup(exported2003)
    _edit(unit.py_path, 'text("Change each system BGM.")', "unknown_command()")
    line = open(unit.py_path, encoding="utf-8").read().split("\n").index(" " * 40 + "unknown_command()") + 1
    r = syncer.sync(unit)
    assert r.action == "error" and ":%d" % line in r.message


def test_new_common_event_without_id_compiles_everything(exported2003):
    syncer, unit, calls, _ = _setup(exported2003)
    _edit(
        unit.py_path,
        '    @common_event(1, "Map Events", trigger="call")',
        '    @common_event(name="Brand new")\n    def brand_new():\n        text("Hi")\n\n'
        '    @common_event(1, "Map Events", trigger="call")',
    )
    r = syncer.sync(unit)
    assert r.action == "import" and "new ids: 2" in r.message  # the first free slot
    assert calls["incremental"] == 0
    # the id is written into the script, which then matches the game
    assert '@common_event(2, name="Brand new")' in open(unit.py_path, encoding="utf-8").read()
    assert syncer.sync(unit).action == "none"


def test_renamed_variable_compiles_everything(exported2003):
    syncer, unit, calls, _ = _setup(exported2003)
    variables = next(u for u in syncer.units() if u.name == "database/variables")
    _edit(variables.py_path, "variable_0001 = Variable(", "first_variable = Variable(")
    _edit(unit.py_path, "variables.variable_0001 = 2", "variables.first_variable = 2")
    syncer.sync(variables)
    r = syncer.sync(unit)  # other events still say variables.variable_0001: they must be compiled again
    assert r.action == "error" and "variable_0001" in r.message
    assert calls["incremental"] == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("    @common_event(3)\n    def a():\n        pass\n", {3: (0, 4)}),
        (  # a decorator wrapped by the formatter
            '    @common_event(\n        4,\n        "Long",\n    )\n    def b():\n        x = 1\n\n        y = 2\n'
            "common_events = CommonEvents()\n",
            {4: (0, 8)},
        ),
        ("    @common_event(name='no id')\n    def c():\n        pass\n", None),
        ("    @common_event(1)\n    def a():\n        pass\n    @common_event(1)\n    def b():\n        pass\n", None),
    ],
)
def test_split_entries(text, expected):
    found = S.split_entries(text, "common_event", "    ")
    assert (found[1] if found else None) == expected


def _map_setup(game):
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    unit = next(u for u in syncer.units() if u.kind == "map" and u.map_id == 1)
    calls = {"incremental": 0}
    incremental = unit.compile_incremental

    def spy(text, base):
        specs = incremental(text, base)
        calls["incremental"] += specs is not None
        return specs

    unit.compile_incremental = spy
    return syncer, unit, calls


def test_map_edit_compiles_only_that_event(exported2003):
    syncer, unit, calls = _map_setup(exported2003)
    before = read_bytes(unit.bin_path)
    text = open(unit.py_path, encoding="utf-8").read()
    other = next(name for name, eid in S._event_function_ids(text).items() if eid not in (None, 9))
    line = next(ln for ln in text.split("\n") if ln.strip() == "variables.variable_0001 = 9999999")
    indent = line[: len(line) - len(line.lstrip())]
    # the edited event refers to an unchanged one by name: its name must still resolve
    _edit(unit.py_path, line, line.replace("9999999", "1234567") + "\n%s%s.move(move_down)" % (indent, other))
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith("changed event 9")
    assert calls["incremental"] == 1
    specs = unit.compile(open(unit.py_path, encoding="utf-8").read())
    assert read_bytes(unit.bin_path) == unit.apply(before, specs)[0]
    assert syncer.sync(unit).action == "none"
