"""DynParams written as plain Python: a variable where RPG Maker takes a number."""

import pytest

from rpgsync.commands import Ctx
from rpgsync.pyexpr import db_names
from rpgsync.script import CompileError, body_lines, compile_body_src

HANDLES = {"variables": {3: "n", 4: "x", 5: "out", 6: "k"}, "switches": {7: "relm"}, "common_events": {}}
CTX = Ctx(handles=HANDLES, dynparams=True)


def _compile(src: str, ctx: Ctx = CTX) -> list[tuple]:
    with db_names(ctx):
        return [(c.code, c.string.decode("cp1252"), c.params) for c in compile_body_src(src.split("\n"), ctx)]


def _roundtrip(src: str) -> list[str]:
    with db_names(CTX):
        cmds = compile_body_src(src.split("\n"), CTX)
        lines, structured = body_lines(cmds, CTX, 0)
        assert structured
        assert compile_body_src(lines, CTX) == cmds
    return lines


def test_switch_through_a_variable():
    assert _compile("if switches[variables.n]:\n    pass") == [
        (12410, "@dynparams_add_param 2, V3", []),
        (12410, "@dynparams_overwrite_next", []),
        (12010, "", [0, 1, 0, 0, 0, 0]),  # placeholder: switch 1
        (10, "", []),
        (22011, "", []),
    ]


def test_placeholder_comment():
    got = _compile("if switches[variables.n]:  # placeholder: switches.relm\n    pass")
    assert got[2] == (12010, "", [0, 7, 0, 0, 0, 0])
    got = _compile("events[variables.n].move(face_up, frequency=8)  # placeholder: this")
    assert got[2][2][0] == 10005


def test_placeholder_after_a_wrapped_call():
    """The formatter moves a trailing comment after the closing parenthesis."""
    got = _compile('pictures[variables.n].show(\n    "a", x=1, y=2\n)  # placeholder: pictures[42]')
    assert got[2][0] == 11110 and got[2][2][0] == 42


def test_only_the_variables_rpg_maker_cannot_take():
    """x=, y= are variables RPG Maker reads itself: only the picture goes through DynParams."""
    got = _compile('pictures[variables.n].show("a", x=variables.x, y=variables.k)')
    assert [g[1] for g in got[:-1]] == ["@dynparams_add_param 1, V3", "@dynparams_overwrite_next"]


@pytest.mark.parametrize(
    ("src", "added"),
    [
        ("common_events[variables.n]()", ["@dynparams_add_param 2, V3"]),
        ("variables.out = actors[variables.n].hp", ["@dynparams_add_param 6, V3"]),
        ('pan_screen("up", variables.n, speed=4)', ["@dynparams_add_param 3, V3"]),
        ("teleport(variables.n, variables.x, 4)", ["@dynparams_add_param 1, V3", "@dynparams_add_param 2, V4"]),
        (
            "teleport(variables.n, variables[variables.x], 4)",
            ["@dynparams_add_param 1, V3", "@dynparams_add_param 2, VV4"],
        ),
        ("if variables[variables.n] > 0:\n    pass", ["@dynparams_add_param 2, V3"]),  # which variable
        (
            'pictures[2].show(f"face{variables.k}_b", x=1, y=2)',
            ["@dynparams_append_string face", "@dynparams_append_number V6", "@dynparams_append_string _b"],
        ),
    ],
)
def test_commands(src, added):
    got = _compile(src)
    assert [g[1] for g in got[: len(added) + 1]] == [*added, "@dynparams_overwrite_next"]


@pytest.mark.parametrize(
    "src",
    [
        'if switches[variables.n]:\n    text("a")',
        'if switches[variables.n]:  # placeholder: switches.relm\n    text("a")\nelse:\n    text("b")',
        "common_events[variables.n]()  # placeholder: common_events[2]",
        "events[variables.n].move(face_up, frequency=8)  # placeholder: this",
        'pictures[variables.n].show(f"fa{{c}}e{variables.k}", x=160, y=120)  # placeholder: pictures[4], "Empty"',
        "variables.out = actors[variables.n].hp",
        "if variables[variables.n] > 0:  # placeholder: variables.x\n    pass",
        'pan_screen("up", variables.n, speed=4)  # placeholder: 3',
    ],
)
def test_decompiled_back(src):
    assert _roundtrip(src) == src.split("\n")


def test_default_placeholder_is_not_written():
    assert _roundtrip("variables.out = actors[variables.n].hp  # placeholder: actors[1]") == [
        "variables.out = actors[variables.n].hp"
    ]


def test_and_condition():
    got = _compile("if switches[variables.n] and switches[3]:  # placeholder: switches.relm\n    pass")
    assert [g[0] for g in got[:4]] == [12410, 12410, 12010, 12010] and got[2][2][1] == 7


@pytest.mark.parametrize(
    ("src", "message"),
    [
        ("wait(variables.n)", "seconds must be a number"),  # stored in tenths: DynParams can't
        ("if switches[variables.n]:  # placeholder: 1, 2\n    pass", "gives 2 values for 1 dynamic value"),
        ("if switches[variables.n]:  # placeholder: party\n    pass", "does not fit"),
    ],
)
def test_errors(src, message):
    with pytest.raises(CompileError, match=message):
        _compile(src)


def test_without_the_plugin():
    with pytest.raises(CompileError, match="can't read switches"):
        _compile("if switches[variables.n]:\n    pass", Ctx(handles=HANDLES))


def test_plain_comment_is_kept():
    """Only `# placeholder:` comments are read; a full line comment stays a Comment command."""
    got = _compile("if switches[variables.n]:  # the chosen clue\n    # placeholder: 9\n    pass")
    assert got[2][2][1] == 1 and (12410, "placeholder: 9", []) in got
