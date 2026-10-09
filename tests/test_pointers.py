"""Variables reached through a pointer: variables[variables.ptr]."""

import pytest

from rpgsync.commands import Ctx
from rpgsync.pyexpr import db_names
from rpgsync.script import CompileError, body_lines, compile_body_src

CTX = Ctx(handles={"variables": {3: "ptr", 4: "slot", 5: "out"}, "switches": {}})


@pytest.mark.parametrize(
    "line",
    [
        "variables.out = variables[variables.ptr]",
        "variables[variables.ptr] = 7",
        "variables[variables.ptr] += variables.slot",
        "variables[variables.ptr] = variables[variables.slot]",
        "switches[variables.ptr] = True",
        "variables[variables[9]] = 1",
    ],
)
def test_pointer_roundtrip(line):
    with db_names(CTX):
        lines, _ = body_lines(compile_body_src([line], CTX), CTX, 0)
    assert lines == [line]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("if variables[variables.ptr] == 0:", "Copy it first"),
        ("if variables.out < variables[variables.ptr]:", "Copy it first"),
        ("if switches[variables.ptr]:", "never read"),
        ("if not switches[variables.ptr]:", "never read"),
        ("variables[variables.ptr + 1] = 0", "compute it in a variable first"),
        ("variables.out = variables[variables.ptr - 1]", "compute it in a variable first"),
    ],
)
def test_pointer_limits_explained(source, message):
    lines = [source] + (["    pass"] if source.endswith(":") else [])
    with db_names(CTX), pytest.raises(CompileError, match=message):
        compile_body_src(lines, CTX)


def test_message_line_holding_a_line_break():
    """Lines pasted with a line break inside stay one line: text(..., split=False)."""
    from rpgsync.lcf import Command

    cmds = [Command(code=10110, string=b"one \nline"), Command(code=20110, string=b"two")]
    with db_names(CTX):
        lines, _ = body_lines(cmds, CTX, 0)
        assert lines[-2].strip() == "split=False,"
        back = compile_body_src(lines, CTX)
    assert [(c.code, c.string) for c in back] == [(c.code, c.string) for c in cmds]
    with db_names(CTX):
        assert len(compile_body_src(['text("one\\ntwo")'], CTX)) == 2  # default: \n starts a line
