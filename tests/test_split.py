"""Scripts too big for one file, written as a folder of files (scriptfiles.py)."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rpgsync import cli
from rpgsync import scriptfiles as F
from rpgsync.lcf import LcfFile
from rpgsync.project import Project
from rpgsync.scaffold import TEST_SCRIPTS_UPDATES, update_test_scripts
from rpgsync.sync import Syncer

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _syncer(game):
    return Syncer(Project(str(game)), log=lambda *_: None)


def _sync_all(syncer):
    return {u.name: syncer.sync(u) for u in syncer.units()}


def _unit(syncer, name):
    return next(u for u in syncer.units() if u.name == name)


def _game_files(game):
    return {p.name: p.read_bytes() for p in Path(game).iterdir() if p.suffix.lower() in (".ldb", ".lmt", ".lmu")}


def _files(folder):
    return {p.name: p.read_bytes() for p in sorted(Path(folder).glob("*.py"))}


def _limit(game, kb):
    (Path(game) / "Scripts" / "pyproject.toml").write_text('[tool.rpgsync]\ngame = ".."\nmax_file_kb = %d\n' % kb)


def _part_with(folder, text):
    return next(p for p in sorted(Path(folder).glob("*.py")) if text in p.read_text(encoding="utf-8"))


def _cli(*args, ok=True):
    r = runner.invoke(cli.app, [str(a) for a in args], env={"FORCE_COLOR": None, "TTY_COMPATIBLE": None})
    out = ANSI.sub("", r.output)
    if ok:
        assert r.exit_code == 0, out
    return out


@pytest.fixture(autouse=True)
def _no_uv_sync(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: None)


@pytest.fixture
def split(exported2003):
    """The 2003 test game, its scripts split at 2 KB by a sync."""
    _limit(exported2003, 2)
    before = _game_files(exported2003)
    results = _sync_all(_syncer(exported2003))
    assert "split common_events.py in" in results["CommonEvents"].message
    assert _game_files(exported2003) == before  # only the scripts changed
    return exported2003


def test_split_scripts_read_as_before(split):
    scripts = split / "Scripts"
    events = scripts / "database" / "common_events"
    assert not (scripts / "database" / "common_events.py").exists()
    assert (events / "__init__.py").exists() and (scripts / "Map0001" / "__init__.py").exists()
    assert (scripts / "database" / "items" / "__init__.py").exists() and (scripts / "database" / "map_tree.py").exists()
    syncer = _syncer(split)
    for u in syncer.units():
        found = u.read_script()
        assert found.text == syncer.state.base_text(u), u.name  # the files join into the script as it was
        if found.package:
            entries = found.entry_files(u.split_style)
            for path, raw in found.raw.items():
                if not path.endswith("__init__.py"):  # each part under the limit, or one entry
                    assert len(raw) <= 2048 or list(entries.values()).count(path) == 1, path
            assert set(found.raw) <= set(u.watched())
    assert all(r.action == "none" and not r.message for r in _sync_all(syncer).values())
    assert "All files are synced" in _cli("status", split)
    head = (events / "__init__.py").read_text(encoding="utf-8")
    assert '"""Common events"""' in head and "from .events_0006_0013 import Events_0006_0013" in head
    assert re.search(
        r"class CommonEvents\(\n(    Events_\d{4}_\d{4},\n)+    CommonEventTable,\n\):\n    class Config:", head
    )
    part = (events / "events_0006_0013.py").read_text(encoding="utf-8")
    assert "from database.common_events import common_events\n\n\nclass Events_0006_0013(CommonEventTable):\n" in part


def test_join_split_round_trip(exported2003):
    syncer = _syncer(exported2003)
    for u in syncer.units():
        found = u.read_script()
        files = F.plan(u.py_path, u.split_style, found.text, None, 1)
        if files is None:
            continue
        joined = F.join(u.py_path, u.split_style, files)
        assert joined.text == found.text, u.name
        assert F.plan(u.py_path, u.split_style, joined.text, joined, 1) == files, u.name  # written again: the same


def test_edit_a_part(split):
    syncer = _syncer(split)
    unit = _unit(syncer, "CommonEvents")
    folder = Path(F.package_dir(unit.py_path))
    before = _files(folder)
    part = _part_with(folder, "Hello from another common event")
    part.write_text(part.read_text(encoding="utf-8").replace("Hello from another", "Hi from another"), encoding="utf-8")
    r = syncer.sync(unit)
    assert r.action == "import" and "common_events/ -> RPG_RT.ldb: changed common event 6" in r.message, r.message
    ce = {it.id: it.struct for it in LcfFile.load(split / "RPG_RT.ldb").root.get("commonevents")}
    assert b"Hi from another common event" in b"".join(bytes(c.string) for c in ce[6].get("event_commands"))
    after = _files(folder)
    assert {n for n in after if after[n] != before.get(n)} == {part.name}  # only that file
    assert syncer.sync(unit).action == "none"
    # an error points into the part, at its line
    text = part.read_text(encoding="utf-8")
    line = text[: text.index('text("Hi from another')].count("\n") + 1
    part.write_text(text.replace('text("Hi from another', 'text(=  "Hi from another', 1), encoding="utf-8")
    r = syncer.sync(unit)
    assert r.action == "error" and r.message.startswith("%s:%d: " % (part, line)), r.message
    out = _cli("status", split)
    assert "database/common_events/%s:%d: " % (part.name, line) in out


def test_entries_stay_where_they_are(split):
    syncer = _syncer(split)
    unit = _unit(syncer, "CommonEvents")
    folder = Path(F.package_dir(unit.py_path))
    # common event 6 moved to the end of the first part, after the ones there
    src = _part_with(folder, "def test_ce():")
    first = sorted(folder.glob("events_*.py"))[0]
    text = src.read_text(encoding="utf-8")
    entry = re.search(r"    @common_event\(6,.*?\n(?=    @common_event|\Z)", text, re.S).group(0)
    src.write_text(text.replace(entry, ""), encoding="utf-8")
    first.write_text(
        first.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + entry.rstrip("\n") + "\n", encoding="utf-8"
    )
    r = syncer.sync(unit)
    assert r.action == "import" and r.message.endswith(": no changes"), r.message
    # the editor adds common event 20: the script is written again from the game
    f = LcfFile.load(split / "RPG_RT.ldb")
    slot = next(it for it in f.root.get("commonevents") if it.id == 20)
    slot.struct.set("name", b"Added Later")
    f.save(split / "RPG_RT.ldb")
    r = syncer.sync(unit)
    assert r.action == "export", r.message
    assert "def test_ce():" in first.read_text(encoding="utf-8")  # still where it was moved
    assert "def test_ce():" not in src.read_text(encoding="utf-8")
    last = _part_with(folder, "@common_event(19,")
    assert '@common_event(20, "Added Later"' in last.read_text(encoding="utf-8")  # after the one before it
    assert syncer.sync(unit).action == "none"


def test_new_entry_stays_in_its_file(split):
    syncer = _syncer(split)
    unit = _unit(syncer, "CommonEvents")
    folder = Path(F.package_dir(unit.py_path))
    part = _part_with(folder, "def test_ce():")
    text = part.read_text(encoding="utf-8")
    head = text.index("    @common_event(6,")
    new = '    @common_event(name="Fresh", trigger="call")\n    def fresh():\n        text("new")\n\n'
    part.write_text(text[:head] + new + text[head:], encoding="utf-8")
    r = syncer.sync(unit)
    assert r.action == "import" and "(new ids: 2)" in r.message, r.message
    assert '    @common_event(2, name="Fresh", trigger="call")\n    def fresh():' in part.read_text(encoding="utf-8")


def test_part_over_the_limit_is_cut(split):
    syncer = _syncer(split)
    unit = _unit(syncer, "CommonEvents")
    folder = Path(F.package_dir(unit.py_path))
    before = _files(folder)
    part = _part_with(folder, "def test_ce():")
    long = "".join('        text("line %d of a long message")\n' % n for n in range(60))
    part.write_text(
        part.read_text(encoding="utf-8").replace('        text("Hello from another common event")\n', long),
        encoding="utf-8",
    )
    r = syncer.sync(unit)
    assert r.action == "import" and "parts over 2 KB cut, new events_" in r.message, r.message
    after = _files(folder)
    assert len(after) > len(before)
    assert all(after[n] == raw for n, raw in before.items() if n not in (part.name, "__init__.py"))
    found = unit.read_script()
    assert found.package and syncer.sync(unit).action == "none"
    assert "test_ce" in (folder / part.name).read_text(encoding="utf-8")  # the first entries stay


@pytest.mark.parametrize(
    "damage, message",
    [
        ("both", "the folder common_events/ holds this script too"),
        ("init", "a script folder needs its __init__.py"),
        ("statement", "a part holds only its class"),
        ("open", "unterminated triple-quoted string literal"),  # each entry is parsed on its own
        ("swallow", "is a bracket or a quote left open above it?"),  # a table is parsed whole
    ],
)
def test_broken_folder_writes_nothing(split, damage, message):
    syncer = _syncer(split)
    unit = _unit(syncer, "database/items" if damage == "swallow" else "CommonEvents")
    folder = Path(F.package_dir(unit.py_path))
    ldb = (split / "RPG_RT.ldb").read_bytes()
    parts = sorted(p for p in folder.glob("*.py") if p.name != "__init__.py")
    part = parts[0] if damage == "swallow" else _part_with(folder, "def test_ce():")
    if damage == "both":
        Path(unit.py_path).write_text(unit.read_script().text, encoding="utf-8")
    elif damage == "init":
        (folder / "__init__.py").unlink()
    elif damage == "statement":
        part.write_text(part.read_text(encoding="utf-8") + "\nprint('outside')\n", encoding="utf-8")
    else:  # a string left open at the end of a part, closed at the end of the next one
        opening, closing = (
            ("        text('''\n", "        ''')\n")
            if damage == "open"
            else ("    note = Item(name='''\n", "    ''')\n")
        )
        part.write_text(part.read_text(encoding="utf-8") + opening, encoding="utf-8")
        nxt = parts[parts.index(part) + 1]
        nxt.write_text(nxt.read_text(encoding="utf-8") + closing, encoding="utf-8")
    r = syncer.sync(unit)
    assert r.action == "error" and message in r.message, r.message
    assert (split / "RPG_RT.ldb").read_bytes() == ldb


def test_map_parts_import_the_events_they_name(split):
    folder = split / "Scripts" / "Map0015"
    imports = [
        (p, m)
        for p in sorted(folder.glob("events_*.py"))
        for m in re.finditer(r"^from \.(\w+) import (.+)$", p.read_text(), re.M)
    ]
    assert imports
    for path, m in imports:
        defined = (folder / (m.group(1) + ".py")).read_text(encoding="utf-8")
        for name in m.group(2).split(", "):
            assert re.search(r"^class %s\(Event\):" % name, defined, re.M), (path, name)
            assert re.search(r"\b%s\." % name, path.read_text(encoding="utf-8").split("\n\n\n", 1)[1]), (path, name)


def test_check_locate_status(split):
    events = split / "Scripts" / "database" / "common_events"
    part = _part_with(events, "def test_ce():")
    assert "common_events/ compiles" in _cli("check", part, "--no-types")
    out = _cli("locate", split, "--common-event", 6).strip()  # its def line, in its part
    assert out == "%s:%d" % (
        part.resolve(),
        part.read_text(encoding="utf-8").split("\n").index("    def test_ce():") + 1,
    )
    part.write_text(part.read_text(encoding="utf-8").replace("Hello from another", "Hi from another"), encoding="utf-8")
    out = _cli("status", split)
    assert re.search(r"modified:\s+database/common_events/\s+\+1 -1\s+common event 6 changed", out), out


def test_first_sync_of_split_scripts(split):
    import shutil

    shutil.rmtree(split / "Scripts" / ".rpgsync")  # a clone of the game's repository
    results = _sync_all(_syncer(split))
    assert results["CommonEvents"].message == "already in sync"
    assert all(r.action == "none" for r in results.values())


def test_map_deleted_with_its_folder(split):
    syncer = _syncer(split)
    tree = next(u for u in syncer.units() if u.kind == "maptree")
    text = Path(tree.py_path).read_text(encoding="utf-8")
    entry = re.search(r"^ *picture_events = Map\(\n(?:.*\n)*? *\)\n", text, re.M).group(0)
    Path(tree.py_path).write_text(text.replace(entry, ""), encoding="utf-8")
    r = syncer.sync(tree)
    assert r.action == "import" and "deleted Map0002.lmu" in r.message, r.message
    assert not (split / "Scripts" / "Map0002").exists() and not (split / "Map0002.lmu").exists()


def test_old_test_scripts_are_updated(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_scripts.py").write_text("\n".join(old for old, _ in TEST_SCRIPTS_UPDATES) + "\n", encoding="utf-8")
    assert update_test_scripts(tmp_path)
    assert (tests / "test_scripts.py").read_text(encoding="utf-8") == "\n".join(
        new for _, new in TEST_SCRIPTS_UPDATES
    ) + "\n"
    assert not update_test_scripts(tmp_path)
