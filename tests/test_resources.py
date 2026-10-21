"""rpgsync check: resource files imported but never used, and references to files not imported."""

import os

from typer.testing import CliRunner

from rpgsync import cli
from rpgsync import script as S
from rpgsync.commands import Ctx
from rpgsync.lcf import Command, MoveCommand, write_move_commands
from rpgsync.resources import Collector, files, key, problems

CTX = Ctx(encoding="cp1252", engine="2k3")


def _game(tmp_path, full=True, **folders):
    """A game folder with these resource files (folder=["name.png", ...])."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "RPG_RT.ini").write_text("[RPG_RT]\nFullPackageFlag=%d\n" % full)
    for folder, names in folders.items():
        for name in names:
            path = tmp_path / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
    return tmp_path


def _event(*commands, graphic=b"", route=()):
    page = S.PageSpec(
        props={**S.PAGE_BASE, "graphic": (graphic, 0), "route": (list(route), True, False)}, commands=list(commands)
    )
    page.lineno = 3
    return S.EventSpec(id=1, name=b"Ev", x=0, y=0, pages=[page], lineno=2)


def _cmd(code, string=b"", params=(), line=7):
    c = Command(code, 0, string, params)
    c.line = line
    return c


def _check(game, *specs, complete=True):
    collector = Collector("cp1252")
    collector.specs(list(specs), "Map0001.py")
    return problems(collector, str(game), "2k3", complete)


def test_names_are_found_as_the_engine_finds_them(tmp_path):
    assert key("Hero.PNG") == key("hero") == "hero"
    assert key("sub\\Door") == key("/sub/door") == "sub/door"
    assert key("mp3.link") == "mp3.link"  # not an extension of a resource
    game = _game(tmp_path, CharSet=["Hero.png", "sub/Door.bmp", "Thumbs.db"], Sound=["Click.WAV"])
    assert files(str(game)) == {
        "CharSet": {"hero": "Hero.png", "sub/door": "sub/Door.bmp"},
        "Sound": {"click": "Click.WAV"},
    }


def test_names_ending_with_a_space(tmp_path):
    """The engine adds the extension to the name as written: "Theme " is Theme .mp3, not Theme.mp3."""
    assert key("Theme ") == "theme " != key("Theme")
    game = _game(tmp_path, Music=["Theme .mp3", "Battle.mp3"])
    found = _check(
        game,
        _event(_cmd(11510, b"Theme ", [0, 100, 100, 50], line=8), _cmd(11510, b"Battle ", [0, 100, 100, 50], line=9)),
    )
    assert [(r.folder, r.name, r.line) for r in found.missing] == [("Music", "Battle ", 9)]
    assert found.unused == {"Music": ["Battle.mp3"]}


def test_missing_and_unused(tmp_path):
    game = _game(tmp_path, Picture=["used.png", "spare.png"], Sound=["Click.wav"])
    found = _check(
        game,
        _event(
            _cmd(11110, b"used", [1] + [0] * 13),
            _cmd(11550, b"click", [100, 100, 50]),
            _cmd(11550, b"(OFF)", [100, 100, 50]),  # no sound
            _cmd(11550, b"(No Sound)", [100, 100, 50]),  # no sound either, when there is no such file
            _cmd(11110, b"(Frame)", [1] + [0] * 13, line=8),  # but a picture
            _cmd(11510, b"Theme", [0, 100, 100, 50], line=9),
        ),
    )
    assert [(r.folder, r.name, r.line) for r in found.missing] == [("Picture", "(Frame)", 8), ("Music", "Theme", 9)]
    assert found.unused == {"Picture": ["spare.png"]}


def test_page_graphics_and_move_routes(tmp_path):
    game = _game(tmp_path, CharSet=["Hero.png", "Ghost.png"], Sound=["Step.wav"])
    route = write_move_commands(
        [MoveCommand(code=34, string=b"Ghost", params=[0]), MoveCommand(code=35, string=b"Step", params=[100, 100, 50])]
    )
    found = _check(game, _event(_cmd(11330, params=[10005, 6, 0, 0, *route]), graphic=b"Hero"))
    assert found.missing == [] and found.unused == {}


def test_names_known_when_the_game_runs(tmp_path):
    game = _game(
        tmp_path, Picture=["num0001.png", "num0002.png", "other.png", "Sheet 001 Page 1.png"], FaceSet=["Hero.png"]
    )
    substitution = [1] + [0] * 17 + [4, 12]  # Show Picture: the last 4 digits from variable 12
    string_var = [0, 0, 0, 1, 5]  # Maniac Patch: the face set of string variable 5
    dynparams = _cmd(12410, b'@dynparams_append_string "oth"')
    unquoted = _cmd(12410, b"@dynparams_append_string Sheet 00")
    commands = _cmd(11110, b"num0000", substitution), _cmd(10130, b"", string_var), dynparams, unquoted
    found = _check(game, _event(*commands))
    assert found.missing == []  # neither num0000 nor the empty name is checked
    assert found.unused == {"FaceSet": ["Hero.png"]}  # num*, oth* and sheet* are names built at runtime


def test_plugin_comments_name_files(tmp_path):
    game = _game(tmp_path, Picture=["hud.png", "boss.png", "a.png"])
    found = _check(game, _event(_cmd(12410, b'@show_hud "hud", 1'), _cmd(22410, b"boss and more")))
    assert found.unused == {"Picture": ["a.png"]}  # a word of a comment, not a part of one


def test_battle_background_only_when_an_image(tmp_path):
    game = _game(tmp_path)
    found = _check(game, _event(_cmd(10710, b"Forest", [0, 1, 0, 0, 0, 0]), _cmd(10710, b"Desert", [0, 1, 1, 0, 0, 0])))
    assert [r.name for r in found.missing] == ["Desert"]


def test_rtp(tmp_path, monkeypatch):
    game = _game(tmp_path / "game", full=False)
    rtp = _game(tmp_path / "rtp", Music=["Theme.mid"])
    monkeypatch.setenv("RPG2K3_RTP_PATH", str(rtp))
    monkeypatch.delenv("RPG_RTP_PATH", raising=False)
    found = _check(game, _event(_cmd(11510, b"Theme", [0, 100, 100, 50]), _cmd(11510, b"Lost", [0, 100, 100, 50])))
    assert [r.name for r in found.missing] == ["Lost"] and found.note is None
    monkeypatch.setenv("RPG2K3_RTP_PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nowhere"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "nowhere"))
    found = _check(game, _event(_cmd(11510, b"Lost", [0, 100, 100, 50])))
    assert found.missing == [] and "no RTP was found" in found.note


def test_partial_check_does_not_list_unused_files(tmp_path):
    game = _game(tmp_path, Picture=["spare.png"])
    assert _check(game, _event(), complete=False).unused == {}


def test_check_warns(exported2003):
    script = exported2003 / "Scripts" / "Map0001.py"
    text = script.read_text(encoding="utf-8")
    lines = text.split("\n")
    at = next(n for n, line in enumerate(lines) if line.startswith("    def page_1(self):"))
    indent = " " * 8
    lines.insert(at + 1, indent + 'pictures[1].show("not_there", x=160, y=120)')
    lines.insert(at + 2, indent + 'pictures[2].show("Not_There", x=160, y=120)')  # the same file: one warning
    script.write_text("\n".join(lines), encoding="utf-8")
    (exported2003 / "Picture").mkdir(exist_ok=True)
    (exported2003 / "Picture" / "spare.png").write_bytes(b"")
    r = CliRunner().invoke(cli.app, ["check", str(exported2003), "--no-types"])
    assert "not imported" not in r.output and "never used" not in r.output  # only with --resources
    r = CliRunner().invoke(cli.app, ["check", str(exported2003), "--no-types", "--resources"])
    out = r.output
    problem = "pictures[...].show() uses Picture/not_there, which is not imported (also used at Map0001.py:%d)"
    assert "Map0001.py:%d: %s" % (at + 2, problem % (at + 3)) in out
    assert "Picture/: 1 imported file never used: spare.png" in out
    assert os.path.basename(str(script)) in out
