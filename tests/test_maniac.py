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
        ("maniac.save(slot=variables[3] + 1)", "unsupported expression"),
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
