"""The command line: first run, `status`, finding the project, timing, consoles."""

import re
import sys

import pytest
from typer.testing import CliRunner

from rpgsync import cli
from rpgsync.lcf import LcfFile

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _cli(*args, ok=True, runner=runner, env=None):
    env = {"FORCE_COLOR": None, "TTY_COMPATIBLE": None, "RPGSYNC_ASCII": None, **(env or {})}
    r = runner.invoke(cli.app, [str(a) for a in args], env=env)
    out = ANSI.sub("", r.output)
    if ok:
        assert r.exit_code == 0, out
    return out


@pytest.fixture(autouse=True)
def _no_uv_sync(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: None)  # no `uv sync` in the scripts folder


@pytest.fixture
def project(exported2003):
    """The 2003 test game, scripts exported and in sync."""
    return exported2003


def _edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_first_run_sets_up_the_scripts_folder(game2003):
    out = _cli("pull", "-y", game2003)
    scripts = game2003 / "Scripts"
    for name in ("pyproject.toml", "tests/test_scripts.py", "Map0001.py", "database/variables.py"):
        assert (scripts / name).exists(), name
    assert "is not synced with rpgsync yet" in out and "leave the game files as they are" in out
    assert "created %s/Scripts (pyproject.toml" % game2003.name in out
    assert re.search(r"Initial sync done: \d+ scripts written in", out)
    assert "wrote Scripts/Map0001.py" not in out  # one summary line, not one per script
    again = _cli("pull", game2003)  # set up once: no question the second time
    assert "not synced with rpgsync yet" not in again and "Initial sync" not in again


def test_first_run_asks_first(game2003, monkeypatch):
    out = _cli("pull", game2003, ok=False)  # no terminal to answer, no -y
    assert "run again with -y" in out
    assert not (game2003 / "Scripts").exists()  # nothing written before the answer
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    r = runner.invoke(cli.app, ["pull", str(game2003)], input="n\n")
    assert "Set up rpgsync for this game? [Y/n]" in r.output
    assert r.exit_code == 1 and not (game2003 / "Scripts").exists()


def test_status_clean(project):
    out = _cli("status", project)
    assert out.startswith("On %s (RPG Maker 2003)" % project.name)
    assert "game:    %s" % project in out
    assert "\u2705 All files are synced" in out


def test_status_script_change(project):
    _edit(project / "Scripts" / "Map0001.py", "variables.variable_0001 = 9999999", "variables[1] = 1234567")
    out = _cli("status", project)
    assert "Changes in scripts" in out and "Changes in the game" not in out
    line = next(ln for ln in out.splitlines() if "Map0001.py" in ln)
    assert re.search(r"modified:\s+Map0001\.py\s+\+1 -1\s+event 9 changed$", line), line
    # --map filters the units
    assert "All files are synced" in _cli("status", project, "--map", "2")


def test_status_compile_error(project):
    _edit(project / "Scripts" / "Map0001.py", "variables.variable_0001 = 9999999", 'variables[1] =+ "x"')
    out = _cli("status", project)
    assert "Scripts with errors" in out
    assert re.search(r"^\s+Map0001\.py:\d+: .*unsupported value", out, re.M), out


def test_status_game_change(project):
    f = LcfFile.load(project / "Map0001.lmu")
    next(it for it in f.root.get("events") if it.id == 9).struct.set("x", 11)
    f.save(project / "Map0001.lmu")
    out = _cli("status", project)
    assert "Changes in the game (`rpgsync pull` updates the scripts):" in out
    assert re.search(r"modified:\s+Map0001\.lmu\s+\+1 -1\s+event 9 changed", out), out
    assert "Changes in scripts" not in out


def test_status_not_exported(project):
    (project / "Scripts" / "Map0002.py").unlink()
    out = _cli("status", project)
    assert re.search(r"Not exported yet.*\n\s+Map0002\.py$", out, re.M), out


@pytest.mark.parametrize("sub", ["", "Scripts", "Scripts/database"])
def test_project_from_current_directory(project, monkeypatch, sub):
    monkeypatch.chdir(project / sub)
    assert _cli("status").startswith("On %s (RPG Maker 2003)" % project.name)


def test_project_from_a_scripts_folder_elsewhere(game2003, tmp_path, monkeypatch):
    from rpgsync.project import Project
    from rpgsync.scaffold import init_scripts_project

    init_scripts_project(Project(str(game2003), str(tmp_path / "ext")))  # [tool.rpgsync] game = "../..."
    monkeypatch.chdir(tmp_path / "ext")
    out = _cli("status")
    assert "scripts: %s" % (tmp_path / "ext") in out and "game:    %s" % game2003 in out


def test_no_project_here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 2 and "no RPG Maker game at" in r.output and "Run rpgsync inside your game folder" in r.output


def test_bare_rpgsync_without_project_prints_help(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["rpgsync"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    captured = capsys.readouterr()
    assert "Usage" in captured.out and "status" in captured.out
    assert "no RPG Maker game at" in captured.err


def test_removed_commands_are_gone():
    for command in ("init", "list", "forget", "sync"):
        assert command not in cli.COMMANDS


def test_push_logs_timing(project):
    _edit(project / "Scripts" / "Map0001.py", "variables.variable_0001 = 9999999", "variables[1] = 1234567")
    out = _cli("push", project, "--map", "1")
    assert re.search(r"Map0001: Map0001\.py -> Map0001\.lmu: changed event 9 \(\d+\.\d\d s\)$", out, re.M), out


EMOJIS = [emoji for emoji, _ in cli.MARKERS.values()]


@pytest.fixture
def accented(exported2003, tmp_path):
    """The test game in a folder with accented and Japanese characters."""
    return exported2003.rename(tmp_path / "Jeu été ゲーム")


def test_accented_names_print_fine(accented):
    out = _cli("status", accented)
    assert out.startswith("On Jeu été ゲーム (RPG Maker 2003)")
    assert "\u2705 All files are synced" in out


def test_legacy_console_gets_ascii_and_no_crash(accented):
    """A Windows console in cp1252: no emoji, no UnicodeEncodeError on Japanese text."""
    cp1252 = CliRunner(charset="cp1252")
    out = _cli("status", accented, runner=cp1252)
    assert out.startswith("On Jeu été ??? (RPG Maker 2003)"), out
    assert "[ok] All files are synced" in out
    _edit(accented / "Scripts" / "Map0001.py", "variables.variable_0001 = 9999999", "variables[1] = 1234567")
    out = _cli("status", accented, runner=cp1252)
    assert "[s] Changes in scripts" in out
    out += _cli("push", accented, "--map", "1", runner=cp1252)
    assert re.search(r"^\[g\] \[\d\d:\d\d:\d\d\] Map0001: Map0001\.py -> Map0001\.lmu: changed event 9", out, re.M)
    assert not any(emoji in out for emoji in EMOJIS)
    assert all(ord(ch) < 0x100 for ch in out)


def test_ascii_markers_can_be_forced(accented):
    out = _cli("status", accented, env={"RPGSYNC_ASCII": "1"})
    assert "[ok] All files are synced" in out and not any(emoji in out for emoji in EMOJIS)


def test_locate_points_at_the_lines_of_events_and_commands(project):
    """`rpgsync locate`: where a script writes an event page and each of its commands."""
    script = project / "Scripts" / "Map0001.py"
    lines = script.read_text(encoding="utf-8").split("\n")
    first = None
    for i, line in enumerate(lines):
        if line.startswith("@event("):
            first = i
            break
    assert first is not None
    event = int(re.match(r"@event\((\d+)", lines[first]).group(1))
    out = _cli("locate", project, "--map", 1, "--event", event).strip()
    path, line = out.rsplit(":", 1)
    assert path == str(script.resolve())
    assert lines[int(line) - 1].lstrip().startswith("def page_")
    # the first command is the first statement of the page
    body = int(line)
    while not lines[body].strip() or lines[body].lstrip().startswith(("#", '"')):
        body += 1
    out = _cli("locate", project, "--map", 1, "--event", event, "--command", 0).strip()
    assert int(out.rsplit(":", 1)[1]) <= body + 1
    # a statement added before it moves it down by one line
    _edit(script, lines[body], lines[body][: len(lines[body]) - len(lines[body].lstrip())] + "wait(0.1)\n" + lines[body])
    moved = _cli("locate", project, "--map", 1, "--event", event, "--command", 1).strip()
    assert int(moved.rsplit(":", 1)[1]) == int(out.rsplit(":", 1)[1]) + 1


def test_locate_common_events_and_errors(project):
    out = _cli("locate", project, "--common-event", 1).strip()
    path, line = out.rsplit(":", 1)
    assert path.endswith("common_events.py") and int(line) > 0
    assert "not in" in _cli("locate", project, "--map", 1, "--event", 9999, ok=False)
    assert "give --map and --event" in _cli("locate", project, ok=False)


def test_locate_troop_pages_and_commands(project):
    """`rpgsync locate --troop T --page P [--command I]`: lines of database/troop_events.py."""
    script = project / "Scripts" / "database" / "troop_events.py"
    lines = script.read_text(encoding="utf-8").split("\n")

    def locate(*args):
        path, line = _cli("locate", project, "--troop", 89, *args).strip().rsplit(":", 1)
        assert path == str(script.resolve())
        return int(line), lines[int(line) - 1].strip()

    assert locate()[1] == "def page_1():"
    assert locate("--page", 2)[1] == "def page_2():"
    assert locate("--page", 2, "--command", 0)[1] == "switches.test_battle_on = False"
    assert locate("--page", 2, "--command", 1)[1] == "# Test Comment. Yeah this code is not that good..."
    assert locate("--page", 2, "--command", 2)[1] == "match show_choices("
    assert locate("--page", 1, "--command", 3)[1].startswith("text(")  # a text over several lines: its first one
    # computed from the file as it is now
    _edit(script, '            text("This is a test battle.")', '            wait(0.1)\n            text("This is a test battle.")')
    lines = script.read_text(encoding="utf-8").split("\n")
    assert locate("--page", 1, "--command", 1)[1] == 'text("This is a test battle.")'
    assert "not in" in _cli("locate", project, "--troop", 1, ok=False)  # no battle events


def test_troop_events_status_push_pull(project):
    script = project / "Scripts" / "database" / "troop_events.py"
    _edit(script, 'text("This is a test battle.")', 'text("This is a battle test.")')
    out = _cli("status", project)
    line = next(ln for ln in out.splitlines() if "troop_events.py" in ln)
    assert re.search(r"modified:\s+database/troop_events\.py\s+\+1 -1\s+troop 89 changed$", line), line
    ldb = (project / "RPG_RT.ldb").read_bytes()
    out = _cli("push", project)
    assert "troop_events.py -> RPG_RT.ldb: changed troop 89" in out
    assert (project / "RPG_RT.ldb").read_bytes() != ldb
    assert "All files are synced" in _cli("status", project)
    # pull --force: back to what the game holds
    _edit(script, 'text("This is a battle test.")', 'text("Not pushed.")')
    _cli("pull", "--force", "-y", project)
    assert 'text("This is a battle test.")' in script.read_text(encoding="utf-8")
