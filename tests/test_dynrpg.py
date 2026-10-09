"""DynRPG plugin calls: dyn.name(...) <-> "@name args" comments."""

import pytest

from rpgsync.commands import CompileError, Ctx
from rpgsync.lcf import Command
from rpgsync.pyexpr import db_names
from rpgsync.script import body_lines, compile_body_src

CTX = Ctx(handles={"variables": {3: "ptr", 152: "clue"}, "switches": {7: "relm"}, "common_events": {}})


def _decompile(comments: list[bytes]) -> list[str]:
    cmds = [Command(code=12410, string=c) for c in comments]
    with db_names(CTX):
        lines, _ = body_lines(cmds, CTX, 0)
        assert compile_body_src(lines, CTX) == cmds
    return lines


@pytest.mark.parametrize(
    ("comment", "line"),
    [
        (b'@f "He said ""hi"""', r'dyn.f("He said \"hi\"")'),
        (b'@f """", ""', r'dyn.f("\"", "")'),
        (b"@f V152, V9, VV3", "dyn.f(variables.clue, variables[9], variables[variables.ptr])"),
        (b"@f N3, NV3", "dyn.f(actors[3].name, actors[variables.ptr].name)"),
        # other spellings of a token keep their form
        (b"@f v152, nv3, VVV3", "dyn.f(v[152], nv[3], VVV[3])"),
    ],
)
def test_arguments(comment, line):
    assert _decompile([comment]) == [line]


def test_unbalanced_quote_stays_a_comment():
    assert _decompile([b'@f """']) == [r'comment("@f \"\"\"")']


def test_argument_kinds_explained():
    with db_names(CTX), pytest.raises(CompileError, match="actors"):
        compile_body_src(["dyn.f(switches.relm)"], CTX)


def test_dynparams_hints():
    lines = _decompile_src(
        [
            "dyn.dynparams_add_param(2, variables.clue)",
            "dyn.dynparams_overwrite_next()",
            "if switches.relm:",
            "    pass",
            "dyn.dynparams_add_param(1, variables.ptr)",
            "dyn.dynparams_add_param(9, 1)",
            "text('placeholder')",
            "dyn.dynparams_overwrite_next()",
            "this.move(move_left)",
        ]
    )
    assert lines[0].endswith("# → ConditionalBranch: switch")
    assert lines[1].endswith("# → ConditionalBranch")
    # built across another command, applied later
    assert lines[4].endswith("# → MoveEvent: event")
    assert lines[5].endswith("# → MoveEvent has no parameter 9 (it has 5)")


def test_dynparams_hints_follow_the_plugin():
    lines = _decompile_src(
        [
            "dyn.dynparams_add_param(7)",  # parameter 5 by default
            "dyn.dynparams_set_index(variables.ptr)",
            "dyn.dynparams_add_param(1)",  # unknown index: no hint
            "dyn.dynparams_overwrite_next()",
            "pictures[1].show('x', x=0, y=0)",
            "if switches.relm:",
            "    dyn.dynparams_add_param(2, 1)",  # applied outside its block: no hint
            "    dyn.dynparams_overwrite_next()",
        ]
    )
    assert lines[0].endswith("# → ShowPicture: scroll_with_map")
    assert "#" not in lines[2]
    assert "#" not in lines[6]


def _decompile_src(src: list[str]) -> list[str]:
    with db_names(CTX):
        cmds = compile_body_src(src, CTX)
        lines, _ = body_lines(cmds, CTX, 0)
        assert compile_body_src(lines, CTX) == cmds
    return lines


# --------------------------------------------------------------------------
# Checks: errors break the game in RPG_RT, warnings do not
# --------------------------------------------------------------------------


def _problems(src: list[str]) -> list[tuple[bool, str]]:
    from rpgsync.dynparams import problems

    with db_names(CTX):
        cmds = compile_body_src(src, CTX)
    return [(p.error, p.message) for p in problems(cmds)]


CHOICES = [
    'match show_choices("A", "B", "C"):',
    '    case "A":',
    "        pass",
    '    case "B":',
    "        pass",
    '    case "C":',
    "        pass",
]


@pytest.mark.parametrize(
    ("src", "error", "message"),
    [
        (
            ["dyn.dynparams_add_param(9, 1)", "dyn.dynparams_overwrite_next()", "this.move(move_left)"],
            True,
            "MoveEvent has no parameter 9 (it has 5)",
        ),
        (
            ["dyn.dynparams_add_param(0, 1)", "dyn.dynparams_overwrite_next()", "this.move(move_left)"],
            True,
            "parameters start at 1",
        ),
        (
            ["dyn.dynparams_message_line_append_string(5, 'x')", "dyn.dynparams_overwrite_next()", "text('a')"],
            True,
            "message lines are 1 to 4",
        ),
        (
            ["dyn.dynparams_choice_case_append_string(0, 'x')", "dyn.dynparams_overwrite_next()", *CHOICES],
            True,
            "choices start at 1",
        ),
        (
            ["dyn.dynparams_message_line_append_string(3, 'x')", "dyn.dynparams_overwrite_next()", "text('a', 'b')"],
            False,
            "the message has 2 line(s): line 3 is not shown",
        ),
        (
            ["dyn.dynparams_message_line_append_string(1, 'x')", "dyn.dynparams_overwrite_next()", "wait(1.0)"],
            False,
            "message lines only change a message, not Wait",
        ),
        (
            ["dyn.dynparams_choice_case_append_string(4, 'x')", "dyn.dynparams_overwrite_next()", *CHOICES],
            False,
            "Show Choices has 3 choice(s): choice 4 is not changed",
        ),
        (
            ["dyn.dynparams_choice_case_append_string(1, 'x')", "dyn.dynparams_overwrite_next()", "wait(1.0)"],
            False,
            "choices only change a Show Choices",
        ),
        (["dyn.dynparams_add_parm(2, 1)"], False, "unknown DynParams command"),
        (["dyn.dynparams_add_param(2, 1)", "wait(1.0)"], False, "never applied"),
        (
            ["if switches.relm:", "    dyn.dynparams_overwrite_next()", "wait(1.0)"],
            False,
            "no command after it in its block",
        ),
    ],
)
def test_dynparams_problem(src, error, message):
    found = _problems(src)
    assert any(e == error and message in m for e, m in found), found


@pytest.mark.parametrize(
    "src",
    [
        # Hertepay: a switch chosen by a variable
        [
            "dyn.dynparams_add_param(2, variables.clue)",
            "dyn.dynparams_overwrite_next()",
            "if switches.relm:",
            "    pass",
        ],
        # built across other commands, applied later (DynParams demo, Tint)
        ["dyn.dynparams_add_param(1, 50)", "wait(1.0)", "dyn.dynparams_overwrite_next()", "tint_screen()"],
        # message and choices shown together
        [
            "dyn.dynparams_message_line_append_string(1, 'x')",
            "dyn.dynparams_choice_case_append_string(3, 'y')",
            "dyn.dynparams_overwrite_next()",
            "text('a')",
            *CHOICES,
        ],
        # cleared on purpose
        ["dyn.dynparams_add_param(2, 5)", "dyn.dynparams_clear_params()"],
        # applied at the next turn of the loop
        ["while True:", "    dyn.dynparams_overwrite_next()", "    wait(1.0)", "    dyn.dynparams_add_param(1, 2)"],
    ],
)
def test_dynparams_fine(src):
    assert _problems(src) == []


def test_only_errors_block_the_sync():
    from rpgsync.compat import check_commands

    with db_names(CTX):
        cmds = compile_body_src(
            [
                "dyn.dynparams_add_parm(2, 1)",
                "dyn.dynparams_add_param(9, 1)",
                "dyn.dynparams_overwrite_next()",
                "wait(1.0)",
            ],
            CTX,
        )
    problems = check_commands(cmds, "2k3", patches=["dynrpg"])
    assert len(problems) == 1 and "Wait has no parameter 9" in problems[0]


# --------------------------------------------------------------------------
# Patches and engine: errors break the game in RPG_RT, warnings do not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "engine", "patches", "error", "message"),
    [
        (Command(code=12410, string=b"@write_text 1"), "2k3", [], False, 'add "dynrpg"'),
        (Command(code=12410, string=b"@easyrpg_add 1, 2"), "2k3", [], False, 'add "dynrpg"'),
        (Command(code=11410, params=[10, 1]), "2k", [], False, "RPG Maker 2000 ignores them"),
        (Command(code=5002), "2k", [], True, "only exists in RPG Maker 2003"),
    ],
)
def test_engine_problem_levels(cmd, engine, patches, error, message):
    from rpgsync.compat import command_problems

    found = [(p.error, p.message) for p in command_problems([cmd], engine, patches)]
    assert any(e == error and message in m for e, m in found), found


def test_easyrpg_comments_need_no_dynrpg():
    from rpgsync.compat import command_problems

    cmds = [Command(code=12410, string=b"@easyrpg_add 1, 2")]
    assert command_problems(cmds, "2k3", ["easyrpg"]) == []
