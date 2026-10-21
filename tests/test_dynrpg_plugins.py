"""DynRPG plugins rpgsync knows: words (word.end), signatures, the game's DynPlugins and DynRPG.ini."""

import inspect
import os

import pytest
from typer.testing import CliRunner

from rpgsync import cli, dsl, dynparams, dynrpg
from rpgsync.commands import CompileError, Ctx
from rpgsync.lcf import Command
from rpgsync.pyexpr import db_names
from rpgsync.script import body_lines, compile_body_src

CTX = Ctx(handles={"variables": {3: "ptr", 152: "clue"}, "switches": {}, "common_events": {}})


def _decompile(comment: bytes) -> str:
    cmds = [Command(code=12410, string=comment)]
    with db_names(CTX):
        lines, _ = body_lines(cmds, CTX, 0)
        assert compile_body_src(lines, CTX) == cmds
    (line,) = lines
    return line


def _compile(*src: str) -> list[Command]:
    with db_names(CTX):
        return compile_body_src(list(src), CTX)


# --------------------------------------------------------------------------
# Words: unquoted text
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comment", "line"),
    [
        (b'@remove_text "sous-menu1", end', 'dyn.remove_text("sous-menu1", word.end)'),
        (
            b'@change_position "blah1", V12, V13, "blah", end',
            'dyn.change_position("blah1", variables[12], variables[13], "blah", word.end)',
        ),
        (b"@dynparams_append_string Simu-Com Faceset 00", 'dyn.dynparams_append_string(word("Simu-Com Faceset 00"))'),
        (b"@dynparams_append_string  Page 2", 'dyn.dynparams_append_string(word(" Page 2"))'),  # the text's space
        (b"@call easyrpg_add, 1, 10", "dyn.call(word.easyrpg_add, 1, 10)"),
        (b'@write_text "a", 1, 2, "x", "fixed", 0', 'dyn.write_text("a", 1, 2, "x", word.fixed, 0)'),  # DynText's word
        (b'@Write_Text "a", 1, 2, "x", "fixed"', 'dyn.Write_Text("a", 1, 2, "x", word.fixed)'),
        (b'@write_text "fixed", 1, 2, "fixed"', 'dyn.write_text("fixed", 1, 2, "fixed")'),  # other arguments: strings
        (b'@write_text "a", 1, 2, "x", Fixed', 'dyn.write_text("a", 1, 2, "x", word.Fixed)'),
        (b"@f end , -x", 'dyn.f(word("end "), word("-x"))'),
        (b"@f class, __init__, caf\xe9", 'dyn.f(word("class"), word("__init__"), word.caf\xe9)'),  # no keyword, dunder
    ],
)
def test_words(comment, line):
    assert _decompile(comment) == line


@pytest.mark.parametrize(
    "comment",
    [
        b'@write_text "CReader7", v39, v40,  "M2 - Science Law", 0, 0, 67',  # two spaces before a string
        b"@f 1,  5",  # spaces around a number: DynRPG drops them, it is no word
        b"@f 1 2",
        b"@f a,b",  # no space after the comma
        b"@f a, , b",  # an empty argument
        b"@f ",
        b"@f 007",
        b'@write_text "a", 1, 2, "x", fixed, 0',  # word.fixed writes "fixed", which DynText reads
    ],
)
def test_what_is_no_word_stays_a_comment(comment):
    assert _decompile(comment).startswith("comment(")


def test_word_forms_mean_the_same():
    assert _compile("dyn.remove_text('x', word.end)") == _compile("dyn.remove_text('x', word('end'))")
    (c,) = _compile('dyn.dynparams_append_string(word(" Page 2"))')
    assert c.string == b"@dynparams_append_string  Page 2"


def test_plugin_words_are_written_as_the_plugin_reads_them():
    (c,) = _compile('dyn.write_text("a", 1, 2, "x", word.fixed)')
    assert c.string == b'@write_text "a", 1, 2, "x", "fixed"'
    assert _compile('dyn.write_text("a", 1, 2, "x", "fixed")') == [c]  # the string is the same
    # another command or another argument: unquoted
    assert [c.string for c in _compile("dyn.f(word.fixed)", 'dyn.write_text(word.fixed, 1, 2, "x")')] == [
        b"@f fixed",
        b'@write_text fixed, 1, 2, "x"',
    ]


@pytest.mark.parametrize("text", ['a"b', "a, b", "a\nb"])
def test_word_refuses_quotes_commas_line_breaks(text):
    with pytest.raises(CompileError, match="can't hold"):
        _compile("dyn.f(word(%r))" % text)


def test_raw_comments_still_compile():
    (c,) = _compile("""comment('@remove_text "sous-menu1", end')""")
    assert c.string == b'@remove_text "sous-menu1", end'


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------


def _warnings(*src: str, patches=("dynrpg",), plugins=None, tables=None) -> list[str]:
    found = dynrpg.problems(_compile(*src), patches, plugins, tables)
    assert not any(p.error for p in found)
    return [p.message for p in found]


@pytest.mark.parametrize(
    ("src", "message"),
    [
        ('dyn.change_text("a", "b")', "dyn.change_text() needs 3 arguments (id, text, color), got 2"),
        ('dyn.write_text("a", 1, 2)', "dyn.write_text() needs at least 4 arguments (id, x, y, text), got 3"),
        ("dyn.dynparams_set_index()", "dyn.dynparams_set_index() needs 1 argument (index), got 0"),
        ('dyn.change_position("a", "10", 20)', 'dyn.change_position(): x (argument 2) is a number, not "10"'),
        ("dyn.remove_text(word.end)", 'dyn.remove_text(): id (argument 1) is a "string", not the word end'),
        ('dyn.change_text("a", "b", "end")', 'color (argument 3) is a number or a word, not "end"'),
        ('dyn.easyrpg_add(1, 2, "x")', 'dyn.easyrpg_add(): argument 3 is a number, not "x"'),
        (
            'dyn.change_position("blah1", variables[12], variables[13], "blah", word.end)',
            "dyn.change_position() reads 3 arguments (id, x, y): 2 more are ignored",
        ),
        ("dyn.remove_all(1)", "dyn.remove_all() reads no argument: 1 more is ignored"),
        ('dyn.change_position("a", actors[1].name, 2)', "x (argument 2) is a number, not N1 (an actor's name)"),
        (
            'comment(\'@write_text "a", 1, 2, "x", fixed\')',  # the DLL compares a "string" with "fixed"
            'fixed (argument 5) works as the "string" "fixed" only (word.fixed), not the word fixed: the plugin ignores',
        ),
        ('dyn.write_text("a", 1, 2, "x", "Fixed")', 'works as the "string" "fixed" only (word.fixed), not "Fixed"'),
    ],
)
def test_signature_problem(src, message):
    found = _warnings(src)
    assert any(message in m for m in found), found


@pytest.mark.parametrize(
    "src",
    [
        'dyn.write_text("a", v[39], v[40], "x", 0, 3, 70)',
        'dyn.write_text("a", 1, 2, "x", word.fixed)',  # written "fixed"
        'dyn.write_text("blah2", 50, 60, "out of 76 battles", "fixed", 4, 98)',  # the string works too
        'dyn.change_text("a", variables[10], word.end)',  # a number as the text
        'dyn.remove_text("a")',
        'dyn.remove_text("a", 0)',  # end: anything
        "dyn.dynparams_add_param(variables.clue)",
        'dyn.dynparams_append_string(word("Simu-Com 0"))',
        'comment("@dynparams_append_string a, b, c")',  # the text after the name, commas included
        'dyn.easyrpg_output(word.Warning, "Variable 5 is $1", variables[5])',
        "dyn.call(word.easyrpg_add, 1, 10)",
        "dyn.easyrpg_add(1, 2, 3, 4)",
        'dyn.some_plugin("x", 1, word.y)',  # unknown: nothing to check
        'comment("@write_text \\"a\\", 1, 2, \\"x\\",, 4")',  # an empty argument: 0
    ],
)
def test_signature_fine(src):
    assert _warnings(src) == []


def test_calls_run_only_with_dynrpg():
    src = ['dyn.write_text("a", 1, 2)', "dyn.easyrpg_add(1)"]
    assert _warnings(*src, patches=()) == []  # plain comments (compat warns about them)
    assert _warnings(*src, patches=("easyrpg",)) == [  # EasyRPG Player alone runs @easyrpg_ only
        "dyn.easyrpg_add() needs at least 2 arguments (variable, value), got 1"
    ]


def test_parse_reads_like_dynrpg():
    name, args = dynrpg.parse('@Write_Text "a ""b""", V1 2 ,NV3, end, -1.5,, "x"')
    assert name == "write_text"
    assert [(a.kind, a.text) for a in args] == [
        ("string", 'a "b"'),
        ("number", "V12"),  # spaces dropped
        ("string", "NV3"),
        ("word", "end"),
        ("number", "-1.5"),
        ("empty", ""),
        ("string", "x"),
    ]
    assert dynrpg.parse('@f "a" b') is None  # something after a string: DynRPG stops
    assert dynrpg.parse("@f 1,")[1][-1].kind == "empty"


# --------------------------------------------------------------------------
# Database ids
# --------------------------------------------------------------------------

TABLES = {"variables": 10, "actors": 3, "items": 5, "skills": 2}


def test_dyntext_codes_name_existing_entries():
    src = r'dyn.write_text("a", 1, 2, r"\v[11] \n[4] \N[3] \i[6] \I[5] \t[3] \T[0] \\v[99] \i[\v[7]]")'
    assert _warnings(src, tables=TABLES) == [
        r"dyn.write_text(): \v[11]: the database has no variable 11 (it has 10)",
        r"dyn.write_text(): \n[4]: the database has no actor 4 (it has 3)",
        r"dyn.write_text(): \i[6]: the database has no item 6 (it has 5)",
        r"dyn.write_text(): \t[3]: the database has no skill 3 (it has 2)",
        r"dyn.write_text(): \T[0]: the database has no skill 0 (it has 2)",
    ]
    assert _warnings(r'dyn.write_text(r"x\v[12]", 1, 2, "")', tables=TABLES)  # the id too
    assert _warnings(r'dyn.some_plugin(r"\v[99]")', tables=TABLES) == []  # not DynText
    assert _warnings(src) == []  # sizes unknown


def test_tokens_name_existing_entries():
    src = "dyn.change_position(actors[4].name, variables[11], variables[variables[12]])"
    assert _warnings(src, tables=TABLES) == [
        "dyn.change_position(): N4: the database has no actor 4 (it has 3)",
        "dyn.change_position(): V11: the database has no variable 11 (it has 10)",
        "dyn.change_position(): VV12: the database has no variable 12 (it has 10)",
    ]


# --------------------------------------------------------------------------
# The game's plugins
# --------------------------------------------------------------------------


def _with(*dlls: str) -> dynrpg.Installed:
    return dynrpg.Installed(dlls=list(dlls))


@pytest.mark.parametrize(
    ("src", "plugins", "patches", "message"),
    [
        ("dyn.process_cmd()", _with("msg_commands.dll"), ("dynrpg",), None),
        (
            "dyn.process_cmd()",
            _with("DynTextPlugin.dll"),
            ("dynrpg",),
            "dyn.process_cmd() comes from msg_commands.dll, which isn't in DynPlugins",
        ),
        (
            "dyn.remove_all()",
            _with(),
            ("dynrpg",),
            "dyn.remove_all() comes from DynTextPlugin.dll, which isn't in DynPlugins (EasyRPG Player emulates it)",
        ),
        ("dyn.remove_all()", _with(), ("dynrpg", "easyrpg"), None),  # made for EasyRPG Player
        ("dyn.remove_all()", _with("dyntextplugin.DLL"), ("dynrpg",), None),  # any case
        ("dyn.foo()", _with("DynTextPlugin.dll"), ("dynrpg",), "dyn.foo(): no plugin in DynPlugins provides it"),
        ("dyn.foo()", _with("DynTextPlugin.dll", "Mystery.dll"), ("dynrpg",), None),  # Mystery.dll may
        (
            "dyn.easyrpg_add(1, 2)",
            _with(),
            ("dynrpg",),
            'is an EasyRPG Player command, RPG_RT ignores it: add "easyrpg"',
        ),
        ("dyn.easyrpg_anything()", _with(), ("dynrpg", "easyrpg"), None),
        ("dyn.call(word.easyrpg_add, 1, 2)", _with(), ("dynrpg", "easyrpg"), None),
        # an unknown dynparams_ command: dynparams.py warns
        ("dyn.dynparams_add_parm(1, 2)", _with("DynParams.dll"), ("dynrpg",), None),
        (
            "dyn.dynparams_add_parm(1, 2)",
            _with(),
            ("dynrpg",),
            "dyn.dynparams_add_parm() comes from DynParams.dll, which isn't in DynPlugins",
        ),
    ],
)
def test_installed_plugins(src, plugins, patches, message):
    found = _warnings(src, patches=patches, plugins=plugins)
    if message is None:
        assert found == []
    else:
        assert any(message in m for m in found), found


def _declared(*names: str, dlls: tuple[str, ...] = ()) -> dynrpg.Installed:
    return dynrpg.Installed(dlls=list(dlls), declared=list(names))


@pytest.mark.parametrize(
    ("src", "plugins", "patches", "message"),
    [
        # a game made for EasyRPG Player: no DLL, EasyRPG.ini lists the plugins
        ("dyn.process_cmd()", _declared("msg_commands"), ("dynrpg",), None),
        ("dyn.process_cmd()", _declared("MSG_COMMANDS.DLL"), ("dynrpg",), None),  # any case, .dll or not
        (
            "dyn.process_cmd()",
            _declared("DynTextPlugin"),
            ("dynrpg",),
            "dyn.process_cmd() comes from msg_commands.dll: add msg_commands to [DynRPG] Plugins in EasyRPG.ini",
        ),
        # the list is all EasyRPG Player runs, emulated plugins too
        (
            "dyn.remove_all()",
            _declared(),
            ("dynrpg", "easyrpg"),
            "dyn.remove_all() comes from DynTextPlugin.dll: add DynTextPlugin to [DynRPG] Plugins in EasyRPG.ini",
        ),
        (
            "dyn.foo()",
            _declared("DynTextPlugin"),
            ("dynrpg",),
            "dyn.foo(): no plugin of EasyRPG.ini's [DynRPG] Plugins provides it",
        ),
        # a game for both: its DLLs (RPG_RT) and the list (EasyRPG Player) must agree
        ("dyn.process_cmd()", _declared("msg_commands", dlls=("msg_commands.dll",)), ("dynrpg",), None),
        (
            "dyn.process_cmd()",
            _declared("game_clock", dlls=("msg_commands.dll", "game_clock.dll")),
            ("dynrpg",),
            "dyn.process_cmd() comes from msg_commands.dll, which EasyRPG.ini's [DynRPG] Plugins doesn't list: "
            "EasyRPG Player doesn't run it",
        ),
        (
            "dyn.process_cmd()",
            _declared("msg_commands", dlls=("game_clock.dll",)),
            ("dynrpg",),
            "dyn.process_cmd() comes from msg_commands.dll, which isn't in DynPlugins: RPG_RT doesn't run it "
            "(EasyRPG Player does)",
        ),
        (
            "dyn.process_cmd()",
            _declared("game_clock", dlls=("game_clock.dll",)),
            ("dynrpg",),
            "dyn.process_cmd() comes from msg_commands.dll, which is neither in DynPlugins nor in EasyRPG.ini's "
            "[DynRPG] Plugins",
        ),
    ],
)
def test_plugins_declared_in_easyrpg_ini(src, plugins, patches, message):
    found = _warnings(src, patches=patches, plugins=plugins)
    if message is None:
        assert found == []
    else:
        assert message in found, found


def test_installed_reads_the_plugins_of_easyrpg_ini(tmp_path):
    ini = tmp_path / "easyrpg.INI"
    ini.write_bytes(b"[Patch]\r\nDynRPG=1\r\n[dynrpg]\r\nplugins = DynTextPlugin, msg_commands.dll  game_clock,\r\n")
    assert dynrpg.installed(str(tmp_path)).declared == ["DynTextPlugin", "msg_commands.dll", "game_clock"]
    ini.write_bytes(b"[DynRPG]\nPlugins=\n")
    assert dynrpg.installed(str(tmp_path)).declared == []  # declared: no plugin runs
    ini.write_bytes(b"[Patch]\nDynRPG=1\n")
    assert dynrpg.installed(str(tmp_path)).declared is None  # EasyRPG Player finds them itself


def test_easyrpg_ini_problems():
    assert dynrpg.easyrpg_ini_problems(dynrpg.Installed(dlls=["game_clock.dll"])) == []
    assert dynrpg.easyrpg_ini_problems(_declared("msg_commands", "game_clock")) == []  # no DLL: EasyRPG Player only
    plugins = _declared(
        "game_clock", "msg_commands", "CortiCustomCrit", dlls=("game_clock.dll", "DynTextPlugin.dll", "Mystery.dll")
    )
    assert dynrpg.easyrpg_ini_problems(plugins) == [
        "[DynRPG] Plugins lists msg_commands, which isn't in DynPlugins: RPG_RT doesn't run it",
        "[DynRPG] Plugins names CortiCustomCrit, no plugin rpgsync knows: EasyRPG Player may not run it",
        "[DynRPG] Plugins doesn't list DynTextPlugin.dll of DynPlugins: EasyRPG Player doesn't run it",
    ]  # Mystery.dll: rpgsync doesn't know it, nor whether EasyRPG Player could run it


def test_dynrpg_ini_problems_with_declared_plugins():
    plugins = dynrpg.Installed(ini=dynrpg.read_ini(INI), declared=["faster_atb_advanced", "game_clock"])
    found = dynrpg.setup_problems(plugins, {"variables": 5000})
    assert found[0] == (
        "[CortiCustomCrit] is read by CortiXCustomCrit.dll, which isn't in DynPlugins or EasyRPG.ini's [DynRPG] Plugins"
    )
    assert found[1].startswith("[Mystery]: no plugin in DynPlugins or EasyRPG.ini's [DynRPG] Plugins")
    assert found[2].startswith("[game_clock] lacks SessionCurrent")  # a declared plugin's settings are checked
    assert len(found) == 3  # [faster_atb_advanced] is read; ExpBug-in isn't declared, so no [Exp_Curves] needed


def test_unknown_game_skips_the_plugin_checks():
    assert _warnings("dyn.process_cmd()", "dyn.foo()", "dyn.easyrpg_add(1, 2)") == []


def test_installed_reads_the_game_folder(tmp_path):
    (tmp_path / "dynplugins").mkdir()
    for name in ("DynTextPlugin.DLL", "game_clock.dll", "readme.txt"):
        (tmp_path / "dynplugins" / name).write_bytes(b"")
    (tmp_path / "DYNRPG.INI").write_bytes(b"[QuickPatches]\r\nA=1\r\n; note\r\n[game_clock]\r\nSessionStart = 4003\r\n")
    found = dynrpg.installed(str(tmp_path))
    assert found.dlls == ["DynTextPlugin.DLL", "game_clock.dll"]
    assert found.ini == {"QuickPatches": {"A": "1"}, "game_clock": {"SessionStart": "4003"}}
    assert dynrpg.installed(str(tmp_path / "nothing")) == dynrpg.Installed()


INI = """[QuickPatches]
KillSwitch=4A43F1,E7030000

[faster_atb_advanced]
BattleSpeedVariable=995
DefaultSpeed=150

[game_clock]
SessionStart=4003

[CortiCustomCrit]
CritCalcType=1
2 = a line without ;
[Mystery]
Key=1
"""


def test_dynrpg_ini_problems():
    ini = dynrpg.read_ini(INI)
    plugins = dynrpg.Installed(dlls=["game_clock.dll", "ExpBug-in.dll", "Mystery.dll"], ini=ini)
    found = dynrpg.setup_problems(plugins, {"variables": 1000})
    assert found[0] == "[faster_atb_advanced] is read by faster_atb_advanced.dll, which isn't in DynPlugins"
    assert found[1] == "[CortiCustomCrit] is read by CortiXCustomCrit.dll, which isn't in DynPlugins"
    assert (
        found[2].startswith("[game_clock] lacks SessionCurrent, SessionSecs")
        and "game_clock.dll needs them" in found[2]
    )
    assert found[3] == (
        "[game_clock] names a variable past the database's 1000 (the editor doesn't show it): SessionStart=4003"
    )
    assert found[4] == (
        "no [Exp_Curves] section: ExpBug-in.dll needs CoefficientA, CoefficientB, CoefficientC, CoefficientD, "
        "CoefficientE"
    )
    assert len(found) == 5  # Mystery.dll: [Mystery] is its section
    plugins = dynrpg.Installed(dlls=["faster_atb_advanced.dll"], ini=ini)
    assert dynrpg.setup_problems(plugins, {"variables": 3327}) == [
        "[game_clock] is read by game_clock.dll, which isn't in DynPlugins",
        "[CortiCustomCrit] is read by CortiXCustomCrit.dll, which isn't in DynPlugins",
        "[Mystery]: no plugin in DynPlugins reads this section",
    ]


def test_a_plugin_reads_its_other_sections():
    ini = dynrpg.read_ini("[CortiCustomCrit]\nCritCalcType=1\n[CortiLog]\nLogToFile=0\n")
    assert dynrpg.setup_problems(dynrpg.Installed(dlls=["CortiXCustomCrit.dll"], ini=ini)) == [
        "[CortiCustomCrit] lacks CritAttackBonusType, CritAttackBonusTypeValue, CritCalcWeaponCriticalType, "
        "CritCalcWeaponCritBonusApplication: CortiXCustomCrit.dll needs them"
    ]


# --------------------------------------------------------------------------
# The table and the DSL agree
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(dynrpg.BY_FUNCTION))
def test_dsl_stub_matches_the_table(name):
    _, fn = dynrpg.BY_FUNCTION[name]
    params = list(inspect.signature(getattr(dsl.DynRPG, name)).parameters.values())[1:]
    positional = [p for p in params if p.kind == p.POSITIONAL_ONLY]
    assert [p.name for p in positional] == [p.name for p in fn.params]
    assert [p.default is not p.empty for p in positional] == [p.optional for p in fn.params]
    assert params[-1].kind == params[-1].VAR_POSITIONAL  # more arguments: accepted, rpgsync check warns


def test_dynparams_commands_are_the_table():
    plugin = dynrpg.BY_DLL["dynparams.dll"]
    assert {f.name for f in plugin.functions} == dynparams.FUNCTIONS


# --------------------------------------------------------------------------
# rpgsync check
# --------------------------------------------------------------------------


def test_table_sizes_count_slots_not_synced_yet(exported2003):
    from rpgsync.project import Project

    proj = Project(str(exported2003))
    sizes = proj.table_sizes()
    assert sorted(sizes) == ["actors", "items", "skills", "variables"] and all(n > 0 for n in sizes.values())
    script = exported2003 / "Scripts" / "database" / "variables.py"
    text = script.read_text(encoding="utf-8")
    script.write_text(text.replace("size = %d " % sizes["variables"], "size = 5000 ", 1), encoding="utf-8")
    assert proj.table_sizes()["variables"] == 5000


def test_check_warns_about_the_plugins(exported2003):
    (exported2003 / "DynPlugins").mkdir()
    (exported2003 / "DynPlugins" / "faster_atb_advanced.dll").write_bytes(b"")
    (exported2003 / "DynRPG.ini").write_text("[QuickPatches]\n[CortiLog]\nLogToFile=0\n")
    r = CliRunner().invoke(cli.app, ["check", str(exported2003), "--no-types", "--patch", "dynrpg"])
    assert r.exit_code == 0, r.output
    assert "DynRPG.ini: [CortiLog] is read by CortiXCustomCrit.dll, which isn't in DynPlugins" in r.output
    assert "DynRPG.ini: no [faster_atb_advanced] section: faster_atb_advanced.dll needs" in r.output
    assert "dyn.write_text() comes from DynTextPlugin.dll, which isn't in DynPlugins" in r.output
    one = os.path.join(exported2003, "Scripts", "Map0001.py")
    r = CliRunner().invoke(cli.app, ["check", one, "--no-types", "--patch", "dynrpg"])
    assert "DynRPG.ini" not in r.output  # only when the whole game is checked


def test_check_reads_the_plugins_of_easyrpg_ini(exported2003):
    """A game made for EasyRPG Player: no DLL, EasyRPG.ini lists its plugins."""
    (exported2003 / "EasyRPG.ini").write_text("[Patch]\nDynRPG=1\n[DynRPG]\nPlugins=DynTextPlugin, Mystery\n")
    r = CliRunner().invoke(cli.app, ["check", str(exported2003), "--no-types", "--patch", "dynrpg"])
    assert r.exit_code == 0, r.output
    assert "dyn.write_text()" not in r.output
    assert "DynPlugins" not in r.output
    assert "EasyRPG.ini: [DynRPG] Plugins names Mystery, no plugin rpgsync knows" in r.output


def test_dynparams_listed_in_easyrpg_ini(exported2003):
    from rpgsync.project import Project

    assert not Project(str(exported2003)).has_dynparams()
    (exported2003 / "EasyRPG.ini").write_text("[DynRPG]\nPlugins=dynparams.dll\n")
    assert Project(str(exported2003)).has_dynparams()
