"""EasyRPG Player's own commands: easyrpg.name(...)."""

import glob
import os

import pytest
from conftest import TESTGAME

from rpgsync import easyrpg
from rpgsync.commands import CompileError, Ctx
from rpgsync.compat import check_commands, command_problems
from rpgsync.lcf import Command, LcfFile
from rpgsync.pyexpr import db_names
from rpgsync.script import body_lines, compile_body_src

CTX = Ctx(handles={"variables": {7: "slot", 8: "done"}, "switches": {4: "night"}, "strings": {3: "save"}})


def roundtrip(code: int, params: list[int], string: bytes = b"", ctx: Ctx = CTX) -> str:
    c = Command(code=code, params=params, string=string)
    with db_names(ctx):
        lines, _ = body_lines([c], ctx, 0)
        back = compile_body_src(lines, ctx)
    assert [(b.code, list(b.params), b.string) for b in back] == [(code, params, string)], lines
    assert len(lines) == 1
    return lines[0]


@pytest.mark.parametrize(
    ("code", "params", "string", "src"),
    [
        # Trigger Event At: the first version has no flags (the event turns to the hero)
        (2002, [0, 11, 0, 1], b"", "easyrpg.trigger_event_at(x=11, y=1)"),
        (2002, [0, 1, 0, 1, 0], b"", "easyrpg.trigger_event_at(x=1, y=1, face_player=False)"),
        (2002, [0, 1, 0, 1, 1], b"", "easyrpg.trigger_event_at(x=1, y=1, face_player=True)"),
        (
            2002,
            [1, 7, 2, 8, 0],
            b"",
            "easyrpg.trigger_event_at(x=variables.slot, y=variables[variables.done], face_player=False)",
        ),
        # Pathfinder
        (
            2003,
            [0, 10001, 1, 19, 1, 20, 0, 500, 0, -1, 18, 0, 0, 0, 8],
            b"",
            "easyrpg.pathfinder(event=player, x=variables[19], y=variables[20], diagonal=True, skippable=True, frequency=8)",
        ),
        (
            2003,
            [0, 10005, 0, 2, 0, 32, 0, 10000, 0, 7, 5, 0, 2, 13, 14, 0, 4],
            b"",
            'easyrpg.pathfinder(event=this, x=2, y=32, search_limit=10000, max_steps=7, while_moving="wait", '
            "debug=True, ignore=[events[13], events[14]], frequency=4)",
        ),
        (
            2003,
            [1, 7, 0, 0, 0, 0, 0, 500, 0, -1, 8, 1, 8, 1, 8],
            b"",
            'easyrpg.pathfinder(event=events[variables.slot], x=0, y=0, while_moving="skip", '
            "ignore=variables[variables.done], frequency=variables.done)",
        ),
        (2003, [0, 16, 0, 1, 0, 2, 0, 500, 0, -1, 0, 0, 0], b"", "easyrpg.pathfinder(event=events[16], x=1, y=2)"),
        # Wait for Single Movement
        (2051, [0, 10001, 0, 0, 0, 0], b"", "easyrpg.wait_for_single_movement(event=player)"),
        (
            2051,
            [0, 16, 0, 100, 0, 8],
            b"",
            "easyrpg.wait_for_single_movement(event=events[16], max_failures=100, result=variables.done)",
        ),
        (
            2051,
            [2, 7, 1, 7, 1, 8],
            b"",
            "easyrpg.wait_for_single_movement(event=events[variables[variables.slot]], max_failures=variables.slot, result=variables[variables.done])",
        ),
        # Set Interpreter Flag: the names as written, or the patch's number
        (2053, [0, 1], b"maniac", 'easyrpg.set_interpreter_flag(patch="maniac", on=True)'),
        (2053, [0, 0], b"rpg2k3-commands", 'easyrpg.set_interpreter_flag(patch="rpg2k3-commands", on=False)'),
        (2053, [1, 7], b"pic-unlock", 'easyrpg.set_interpreter_flag(patch="pic-unlock", on=variables.slot)'),
        (2053, [0, 1, 3], b"", "easyrpg.set_interpreter_flag(patch=3, on=True)"),
        # Process JSON: the path, then the JSON, when the command holds both
        (
            2055,
            [0, 0, 1, 1, 0, 2, 0, 2, 0, 14, 4],
            b"/array_example",
            'easyrpg.process_json(op="get", json=strings[1], path="/array_example", target=strings[2], pretty=True)',
        ),
        (
            2055,
            [0, 0, 0, 8, 0, 1, 0, 8, 0, 2, 0],
            b'/a{"a": 1}',
            'easyrpg.process_json(op="get", json="{\\"a\\": 1}", path="/a", target=variables.done)',
        ),
        (
            2055,
            [0, 3, 2, 7, 0, 2, 1, 7, 2, 7, 12],
            b"",
            'easyrpg.process_json(op="get_keys", json=strings[variables.slot], path=strings[variables.slot], '
            "target=strings[variables.slot], pretty=True, keys_object=True)",
        ),
        (
            2055,
            [0, 8, 1, 3, 0, 0, 0, 4, 0, 0, 0],
            b"",
            'easyrpg.process_json(op="contains", json=strings.save, target=switches.night)',
        ),
        (
            2055,
            [1, 7, 3, 3, 0, 0, 0, 0, 1, 3, 0],
            b"",
            "easyrpg.process_json(op=variables.slot, json=strings[variables[variables[3]]], path=strings.save)",
        ),
        (
            2055,
            [0, 0, 1, 1, 0, 2, 0, 2, 0, 6, 1],
            b"/\\v[3]",
            'easyrpg.process_json(op="get", json=strings[1], path=r"/\\v[3]", target=strings[2], expand=True)',
        ),
        # Clone / Destroy Map Event
        (
            2056,
            [0, 10, 0, 3, 0, 7, 0, 2, 0, 0, 0, 0],
            b"Text Tester",
            'easyrpg.clone_map_event(map=10, event=3, x=7, y=2, name="Text Tester")',
        ),
        (
            2056,
            [1, 9, 1, 10, 1, 11, 1, 12, 1, 13, 1, 3],
            b"",
            "easyrpg.clone_map_event(map=variables[9], event=variables[10], x=variables[11], y=variables[12], "
            "new_id=variables[13], name=strings.save)",
        ),
        (2056, [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0], b"", "easyrpg.clone_map_event(event=1, x=0, y=0)"),
        (2057, [0, 0], b"", "easyrpg.destroy_map_event()"),
        (2057, [0, 10005], b"", "easyrpg.destroy_map_event(event=this)"),
        (2057, [1, 8], b"", "easyrpg.destroy_map_event(event=events[variables.done])"),
        # EasyRPG Player's scenes in the Maniac Patch's Call System Functions
        (11910, [207], b"", 'call_system_function("language")'),
        (11910, [200], b"", 'call_system_function("main_menu")'),
    ],
)
def test_named_forms(code, params, string, src):
    assert roundtrip(code, params, string) == src


def test_extra_parameters():
    assert roundtrip(2057, [1, 8, 5]) == "easyrpg.destroy_map_event(event=events[variables.done], extra=(5,))"
    assert (
        roundtrip(2002, [0, 1, 0, 2, 1, 9, 9]) == "easyrpg.trigger_event_at(x=1, y=2, face_player=True, extra=(9, 9))"
    )
    assert roundtrip(2053, [0, 1, 4], b"maniac") == 'easyrpg.set_interpreter_flag(patch="maniac", on=True, extra=(4,))'
    src = "easyrpg.pathfinder(event=player, x=1, y=2, frequency=3, extra=(7,))"
    assert roundtrip(2003, [0, 10001, 0, 1, 0, 2, 0, 500, 0, -1, 0, 0, 0, 0, 3, 7]) == src


@pytest.mark.parametrize(
    ("code", "params", "string", "src"),
    [
        # a patch name in another case (EasyRPG Player reads it), or no patch at all
        (2053, [0, 1], b"Maniac", 'easyrpg.set_interpreter_flag(0, 1, text="Maniac")'),
        (2053, [0, 1, 9], b"", "easyrpg.set_interpreter_flag(0, 1, 9)"),
        # Pathfinder waiting and skipping while the event moves; a cut list of events ignored
        (
            2003,
            [0, 10005, 0, 0, 0, 0, 0, 0, 0, 0, 9, 0, 0],
            b"",
            "easyrpg.pathfinder(0, 10005, 0, 0, 0, 0, 0, 0, 0, 0, 9, 0, 0)",
        ),
        (
            2003,
            [0, 10005, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 4],
            b"",
            "easyrpg.pathfinder(0, 10005, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 4)",
        ),
        # Process JSON: a length that isn't the text's, the target's kind from a variable
        (
            2055,
            [0, 0, 0, 5, 0, 1, 0, 8, 0, 1, 0],
            b"/{}",
            'easyrpg.process_json(0, 0, 0, 5, 0, 1, 0, 8, 0, 1, 0, text="/{}")',
        ),
        (2055, [0, 0, 1, 1, 1, 7, 0, 8, 0, 0, 0], b"", "easyrpg.process_json(0, 0, 1, 1, 1, 7, 0, 8, 0, 0, 0)"),
        # a value read from a switch (mode 3): EasyRPG Player reads it, the named forms don't
        (2057, [3, 4], b"", "easyrpg.destroy_map_event(3, 4)"),
        # fewer parameters than EasyRPG Player reads
        (2051, [0, 10001], b"", "easyrpg.wait_for_single_movement(0, 10001)"),
    ],
)
def test_raw_forms(code, params, string, src):
    assert roundtrip(code, params, string) == src


def test_every_easyrpg_command_has_a_name():
    assert roundtrip(2050, [1, 2], b"a") == 'easyrpg.call_movement_action(1, 2, text="a")'
    assert roundtrip(2052, [0, 1]) == "easyrpg.animate_variable(0, 1)"
    assert roundtrip(2058, [4]) == "easyrpg.string_picture_menu(4)"
    assert roundtrip(2052, []) == "easyrpg.animate_variable()"
    assert roundtrip(2099, [1]) == "easyrpg.command_2099(1)"
    # no parameters: would read as the named form with its defaults
    assert roundtrip(2057, []) == "cmd(2057)"


def test_lengths_count_characters():
    """Process JSON counts the characters of its texts (Unicode code points), not bytes."""
    ctx = CTX.model_copy(update={"encoding": "utf-8"})
    text = '/é{"é": 1}'
    src = 'easyrpg.process_json(op="get", json="{\\"é\\": 1}", path="/é", target=variables.done)'
    assert roundtrip(2055, [0, 0, 0, 8, 0, 1, 0, 8, 0, 2, 0], text.encode("utf-8"), ctx) == src


@pytest.mark.parametrize(
    ("src", "message"),
    [
        ("easyrpg.trigger_event_at(x=1)", "missing the argument 'y'"),
        ("easyrpg.trigger_event_at(x=items[1], y=2)", "x is a number or variables"),
        ("easyrpg.trigger_event_at(x=1, y=2, extra=(1,))", "comes after face_player"),
        ("easyrpg.pathfinder(event=3, x=1, y=2)", "event is this, player"),
        ('easyrpg.pathfinder(event=player, x=1, y=2, while_moving="wiat")', "while_moving is"),
        ("easyrpg.pathfinder(event=player, x=1, y=2, ignore=variables[3])", "ignore is a list of events"),
        ("easyrpg.pathfinder(event=player, x=1, y=2, ignore=[events[variables[3]]])", "listed by id"),
        ("easyrpg.wait_for_single_movement(event=player, result=switches[2])", "result is None, variables"),
        ('easyrpg.set_interpreter_flag(patch="Maniac", on=True)', "patch is one of"),
        ("easyrpg.set_interpreter_flag(patch=9, on=True)", "patch is one of"),
        ('easyrpg.process_json(op="gett", json=strings[1])', "op is one of"),
        ('easyrpg.process_json(op="get", json=strings[1], path=strings[variables[variables[2]]])', "path is a text"),
        ('easyrpg.process_json(op="get", json=strings[1], target=items[2])', "target is switches"),
        ("easyrpg.clone_map_event(event=1, x=0, y=0, name=7)", "name is a string"),
        ("easyrpg.destroy_map_event(event=2)", "event is this, player"),
        ("easyrpg.nope()", "unknown EasyRPG command"),
        ("easyrpg.destroy_map_event(0, x=1)", "raw parameters"),
        ('call_system_function("language", 1)', "takes no option"),
        ('call_system_function("langage")', "function is one of"),
    ],
)
def test_errors(src, message):
    with db_names(CTX), pytest.raises(CompileError, match=message):
        compile_body_src([src], CTX)


def test_names_and_codes():
    for code, name in easyrpg.NAMES.items():
        assert easyrpg.code_of(name) == code
    assert easyrpg.code_of("command_2099") == 2099
    assert easyrpg.code_of("command_3001") is None


def test_stubs_match_the_named_forms():
    """Every EasyRPG command has a typed stub, taking exactly its named form's arguments."""
    import inspect

    from rpgsync import dsl

    for code in easyrpg.NAMES:
        assert hasattr(dsl.EasyRPG, easyrpg.name_of(code)), code
    for code, keys in easyrpg.ARGS.items():
        params = inspect.signature(getattr(dsl.EasyRPG, easyrpg.name_of(code))).parameters
        assert [p for p in params if p not in ("self", "params", "text")] == keys, code
    assert set(easyrpg.ARGS) == set(easyrpg.DECODERS)
    assert set(dsl.SystemFunction.__args__) >= set(easyrpg.SYSTEM_FUNCTIONS.values())


def test_engine_check_names_the_command():
    c = Command(code=2055, params=[0] * 11)
    (problem,) = check_commands([c], "2k3")
    assert problem.startswith('EasyRPG command easyrpg.process_json(): add "easyrpg" to [tool.rpgsync] patches')
    assert check_commands([c], "2k3", patches=["easyrpg"]) == []
    assert check_commands([c], "2k3", allow=[2055]) == []


@pytest.mark.parametrize(
    ("code", "params", "option"),
    [
        (2057, [2, 3], "a value through variables[variables[n]]"),
        (2003, [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 3, 2, 4], "a value through variables[variables[n]]"),
        (2055, [0, 0, 1, 1, 0, 2, 0, 2, 0, 0, 0], "a string variable as target"),
        (2055, [0, 0, 3, 1, 0, 1, 0, 2, 0, 0, 0], "the JSON in strings[variables[variables[n]]]"),
        (2056, [0] * 10 + [1, 3], "the name from a string variable"),
    ],
)
def test_maniac_options_of_easyrpg_commands_are_reported(code, params, option):
    c = Command(code=code, params=params)
    (problem,) = command_problems([c], "2k3", patches=["easyrpg"])
    assert not problem.error and "uses a Maniac Patch option (%s)" % option in problem.message
    assert command_problems([c], "2k3", patches=["easyrpg", "maniac"]) == []


def test_easyrpg_scenes_without_the_patch_are_reported():
    c = Command(code=11910, params=[207])
    (problem,) = command_problems([c], "2k3", patches=["maniac"])
    assert not problem.error and "uses an EasyRPG Player option (Call System Functions 207" in problem.message
    assert command_problems([c], "2k3", patches=["maniac", "easyrpg"]) == []


def test_the_test_game_has_named_forms(tmp_path):
    """Every EasyRPG command of the EasyRPG test game is written in its named form (the
    round trip itself: tests/test_roundtrip.py)."""
    from rpgsync import script as S
    from rpgsync.project import Project

    game = os.path.join(TESTGAME, "TestGame-EasyRPG")
    if not os.path.isdir(game):
        pytest.skip("TestGame-EasyRPG not found")
    ctx = Project(game, script_dir=str(tmp_path)).context()
    found = 0
    for path in sorted(glob.glob(os.path.join(game, "*.lmu"))):
        for spec in S.map_event_specs(LcfFile.load(path).root, ctx):
            for page in spec.pages:
                for c in page.commands:
                    if c.code in easyrpg.CODES:
                        found += 1
                        with db_names(ctx):
                            assert roundtrip(c.code, c.params, c.string, ctx).startswith(
                                "easyrpg.%s(" % easyrpg.name_of(c.code)
                            )
                            assert easyrpg.DECODERS[c.code](c, ctx) is not None, (path, c)
    assert found >= 290
