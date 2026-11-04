"""Maniac Patch commands: maniac.name(...)."""

import pytest

from rpgsync import maniac
from rpgsync.commands import CompileError, Ctx
from rpgsync.compat import check_commands
from rpgsync.lcf import Command
from rpgsync.pyexpr import db_names
from rpgsync.script import body_lines, compile_body_src

CTX = Ctx(handles={"variables": {7: "slot", 8: "ok"}, "switches": {4: "night"}})


def roundtrip(code: int, params: list[int], string: bytes = b"") -> str:
    c = Command(code=code, params=params, string=string)
    with db_names(CTX):
        lines, _ = body_lines([c], CTX, 0)
        back = compile_body_src(lines, CTX)[0]
    assert (back.code, list(back.params), back.string) == (code, params, string), lines
    assert len(lines) == 1
    return lines[0]


@pytest.mark.parametrize(
    ("code", "params", "src"),
    [
        (3005, [10, 11], "maniac.get_mouse_position(x=variables[10], y=variables[11])"),
        (3002, [0, 3, 1, 8], "maniac.save(slot=3, result=variables.ok)"),
        (3002, [1, 7, 0, 0], "maniac.save(slot=variables.slot)"),
        (3002, [2, 7, 0, 0], "maniac.save(slot=variables[variables.slot])"),
        (3002, [3, 4, 0, 0], "maniac.save(slot=switches.night)"),
        (3003, [0, 2, 1], "maniac.load(slot=2, skip_check=True)"),
        (3004, [], "maniac.end_load_process()"),
        (3006, [1, 5, 6], "maniac.set_mouse_position(x=variables[5], y=variables[6])"),
    ],
)
def test_named_forms(code, params, src):
    assert roundtrip(code, params) == src


def test_unknown_selector_keeps_the_raw_parameters():
    assert roundtrip(3002, [5, 1, 0, 0]) == "maniac.save(5, 1, 0, 0)"


def test_every_maniac_command_has_a_name():
    assert roundtrip(3036, [1, -2]) == "maniac.control_self_variable(1, -2)"
    assert roundtrip(3037, [4]) == "maniac.command_3037(4)"
    assert roundtrip(3020, [0, 1, 2], b"\x01a") == 'maniac.control_strings(0, 1, 2, text="\\x01a")'


def test_extra_parameters():
    assert roundtrip(3005, [1, 2, 99]) == "maniac.get_mouse_position(x=variables[1], y=variables[2], extra=(99,))"


@pytest.mark.parametrize(
    ("src", "message"),
    [
        ("maniac.set_mouse_position(x=5, y=variables[3])", "share how they are given"),
        ("maniac.save(slot=variables[3] + 1)", "slot is a number or variables"),
        ("maniac.save(slot=items[3])", "slot is a number or variables"),
        ("maniac.nope()", "unknown Maniac command"),
        ("maniac.control_strings(0, x=1)", "raw parameters"),
    ],
)
def test_errors(src, message):
    with db_names(CTX), pytest.raises(CompileError, match=message):
        compile_body_src([src], CTX)


def test_engine_check_names_the_command():
    problems = check_commands([Command(code=3020, params=[0])], "2k3")
    assert problems == ['Maniac command maniac.control_strings(): add "maniac" to [tool.rpgsync] patches']
    assert check_commands([Command(code=3020, params=[0])], "2k3", patches=["maniac"]) == []


def test_names_and_codes():
    for code, name in maniac.NAMES.items():
        assert maniac.code_of(name) == code
    assert maniac.code_of("command_3038") == 3038


def test_stubs_match_the_specs():
    """Every Maniac command has a typed stub taking its named arguments."""
    import inspect

    from rpgsync import dsl

    for code in maniac.NAMES:
        assert hasattr(dsl.ManiacPatch, maniac.name_of(code)), code
    for spec in maniac.SPECS:
        params = inspect.signature(getattr(dsl.ManiacPatch, spec.name)).parameters
        pos, kw = spec.arg_names()
        assert set(pos + kw) <= set(params), (spec.name, set(pos + kw) - set(params))


# --------------------------------------------------------------------------
# Typed forms of the Maniac commands and of the Maniac options of standard commands
# --------------------------------------------------------------------------

MCTX = Ctx(
    handles={"variables": {7: "slot", 8: "ok"}, "switches": {4: "night"}, "strings": {3: "name"}},
    maniac_layout=True,
)


def mroundtrip(cmds: list[Command], ctx: Ctx = MCTX) -> list[str]:
    with db_names(ctx):
        lines, structured = body_lines(cmds, ctx, 0)
        back = compile_body_src(lines, ctx)
    assert structured
    assert [(c.code, list(c.params), c.string, c.indent) for c in back] == [
        (c.code, list(c.params), c.string, c.indent) for c in cmds
    ], lines
    return lines


def C(code: int, params: list[int], string: bytes = b"", indent: int = 0) -> Command:
    return Command(code=code, params=params, string=string, indent=indent)


@pytest.mark.parametrize(
    ("code", "params", "string", "src"),
    [
        # the Maniac editor's layout of standard commands, and the other one marked
        (10220, [0, 1, 0, 0, 0, 456], b"", "variables[1] = 456"),
        (10220, [0, 1, 1, 0, 0, 456, 0], b"", "variables[1] = 456  # layout: rpg_maker"),
        (10220, [0, 1, 0, 0, 19, 1, 7, 2, 1], b"", "variables[1] = variables.slot + 2"),
        (10210, [0, 4, 0, 0], b"", "switches.night = True"),
        (10120, [0, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0], b"", 'message_options(position="bottom", avoid_hero=True)'),
        (10130, [8], b"Actor2", 'face("Actor2", 8)'),
        # Maniac options of standard commands
        (11410, [10], b"", "wait(1.0, key=None)"),
        (11410, [30, 256, 0], b"", "wait(frames=30)"),
        (11410, [7, 256, 1], b"", "wait(frames=variables.slot)"),
        (11410, [7, 0, 2], b"", "wait(tenths=variables[variables.slot])"),
        (12330, [3, 7, 0], b"", "common_events[variables.slot]()"),
        (12330, [4, 7, 16], b"", "common_events[variables[variables.slot]](page=16)"),
        (12320, [0, 1, 0], b"", "this.restore()"),
        (12320, [16, 0, 7], b"", "events[variables.slot].erase()"),
        (12320, [0, 1, 3], b"", "events[3].restore()"),
        (12220, [1, 0], b"", "continue_loop()"),
        (12220, [0, 2], b"", "break_loop(levels=3)"),
        (11910, [3, 2], b"", 'call_system_function("screen_mode", "fullscreen")'),
        (11910, [1, 0], b"", 'call_system_function("load")'),
        (11930, [-1, 17], b"", "disable_system_functions(save=True, options=True)"),
        (
            10120,
            [0, 2, 0, 0, 0x100, 320, 80, 3, 0, 0, 0],
            b"",
            'message_options(position="bottom", width=320, height=80, font=strings.name)',
        ),
        (10130, [1, 1, 0, 17, 3], b"", "face(strings.name, variables[1], right=True)"),
        (11510, [0, 100, 7, 50, 0x1000], b"Town", 'play_bgm("Town", tempo=variables.slot)'),
        (11550, [100, 100, 50, 1, 3], b"", "play_se(strings.name)"),
        # new Maniac commands
        (3033, [0, 0, 0, 0, 0], b"", "maniac.console_open()"),
        (3033, [0, 2, 5, 0, 0], b"Hi \\v[1]", 'maniac.console_print(r"Hi \\v[1]", expand=True, no_newline=True)'),
        (3033, [256, 2, 0, 3, 0], b"", "maniac.console_print(strings.name)"),
        (3033, [0x1000, 3, 1, 3, 7], b"", "maniac.console_input(strings.name, max_length=variables.slot, expand=True)"),
        (3033, [0x1100, 4, 24, 7, 8], b"", "maniac.console_color(foreground=variables.slot, background=variables.ok)"),
        (3038, [0, 0, 1, 7], b"", 'maniac.get_media_option("window_zoom", into=variables.slot)'),
        (3038, [256, 1, 7, 8], b"", 'maniac.set_media_option("show_fps", variables.ok)'),
        (
            3007,
            [0, 1, 160, 120, 100, 0, 100, 100, 100, 100, 0, 0, 0, 256, 262145, 7, 0, 97, 0, 0, 12],
            b"\x01Hello\x01System\x01",
            'maniac.show_string_picture(picture=1, x=160, y=120, text="Hello", system="System")',
        ),
        (
            3007,
            [0x1000010, 2, 7, 8, 100, 0, 100, 100, 100, 100, 0, 0, 1, 0x7000, 262145, 7, 0, 97, 0, 0, 12],
            b"\x01Hi\x01\x01",
            "maniac.show_string_picture(origin=1, picture=2, x=variables.slot, y=variables.ok, "
            'blend="multiply", stretch="none", no_frame=True, no_gradient=True, no_shadow=True, text="Hi")',
        ),
    ],
)
def test_typed_maniac_forms(code, params, string, src):
    assert mroundtrip([C(code, params, string)]) == [src]


def test_string_picture_from_a_string_variable_stays_raw():
    (line,) = mroundtrip([C(3007, [0, 1, 0, 0, 100, 0, 100, 100, 100, 100] + [0] * 11, b"\x02\x01\x01")])
    assert line.startswith("maniac.show_string_picture(0, 1, ")


@pytest.mark.parametrize(
    ("params", "header"),
    [
        ([0, 0, 0, 0, 0], "while True:"),
        ([], "while True:  # layout: rpg_maker"),
        ([1, 0, 10, 0, 0], "for _ in range(10):"),
        ([1, 1, 8, 0, 7], "for variables.slot in range(variables.ok):"),
        ([2, 0, 1, 5, 7], "for variables.slot in range(1, 6):"),
        ([2, 0x10, 1, 8, 7], "for variables.slot in range(1, variables.ok + 1):"),
        ([3, 0, 10, 1, 0], "for _ in range(10, 0, -1):"),
        ([4, 0x401, 7, 10, 0], "while variables.slot < 10:"),
    ],
)
def test_loops(params, header):
    cmds = [C(12210, params), C(10220, [0, 1, 0, 1, 0, 1], indent=1), C(10, [], indent=1), C(22210, params)]
    lines = mroundtrip(cmds)
    assert lines[0] == header
    assert lines[1] == "    variables[1] += 1"


def test_continue_in_a_loop():
    cmds = [C(12210, [0, 0, 0, 0, 0]), C(12220, [1, 0], indent=1), C(10, [], indent=1), C(22210, [0, 0, 0, 0, 0])]
    assert mroundtrip(cmds) == ["while True:", "    continue"]


def test_script_lines():
    cmds = [C(3030, [0, 0, 0, 0, 2, 2, 7, 4, 3], b"v[7] = 1;"), C(3031, [], b"t[3] = 'a';")]
    assert mroundtrip(cmds) == ['maniac.script("v[7] = 1;", "t[3] = \'a\';", results=[variables.slot, strings.name])']
    assert mroundtrip([C(3030, [1, 3, 0, 0, 0])]) == ["maniac.script(strings.name)"]


def test_layout_detection(tmp_path):
    from rpgsync import layout as L

    assert L.statement_layout(["variables[1] = 2  # layout: maniac"]) == "maniac"
    assert L.statement_layout(["variables[1] = 2  # a note"]) is None
    with pytest.raises(ValueError, match="unknown layout"):
        L.statement_layout(["variables[1] = 2  # layout: other"])
    c = C(10220, [0, 1, 1, 0, 0, 5, 0])
    assert L.to_maniac(c).params == [0, 1, 0, 0, 0, 5]
    assert L.to_standard(L.to_maniac(c)).params == c.params


@pytest.mark.parametrize(
    ("src", "message"),
    [
        ("for variables[1] in range(1, variables[2]):", "the end of the range"),
        ("for i in range(3):", "counts in variables"),
        ("while variables[1] < items[2]:", "right side"),
        ("wait(frames=30, key=True)", "takes no other argument"),
        ("events[10001].erase()", None),
        ("player.erase()", "map events"),
    ],
)
def test_maniac_extension_errors(src, message):
    body = src + ("\n    pass" if src.endswith(":") else "")
    with db_names(MCTX):
        if message is None:
            compile_body_src(body.split("\n"), MCTX)
            return
        with pytest.raises(CompileError, match=message):
            compile_body_src(body.split("\n"), MCTX)


def test_dynparams_games_keep_their_forms():
    ctx = Ctx(handles={"variables": {3: "n"}}, dynparams=True)
    with db_names(ctx):
        cmds = compile_body_src(["common_events[variables.n]()  # placeholder: common_events[2]"], ctx)
    assert [c.code for c in cmds] == [12410, 12410, 12330]


def test_maniac_options_without_the_patch_are_reported():
    from rpgsync.compat import command_problems

    loop = Command(code=12210, params=[1, 0, 10, 0, 0])
    (problem,) = command_problems([loop], "2k3")
    assert not problem.error and "Maniac Patch option (loop kind" in problem.message
    assert command_problems([loop], "2k3", patches=["maniac"]) == []
    # the Maniac editor's layout of a plain command is no Maniac option
    assert command_problems([Command(code=12210, params=[0, 0, 0, 0, 0])], "2k3") == []
    assert command_problems([Command(code=10220, params=[0, 1, 0, 0, 0, 5])], "2k3") == []
