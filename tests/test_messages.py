"""Message window checks (rpgsync.messages)."""

import pytest

from rpgsync.commands import Ctx
from rpgsync.lcf import Command
from rpgsync.messages import MessageLimits, command_problems, text_width

ACTORS = {1: "Alex", 3: "Haru"}


@pytest.mark.parametrize(
    "line,width",
    [
        ("Bonjour", 7),
        (r"\c[2]Haru\c[0].", 5),  # colours take no space
        (r"\s[3]vite\!\.\|\^\>\<\$", 4),  # speed, waits, flow, gold window
        (r"\n[3] : salut", 4 + 8),  # the actor's name
        (r"\N[0]", 4),  # the leader: the longest name
        (r"\v[12] pièces", 3 + 7),  # a variable counts as 3 characters
        (r"\c[\v[3]]ok", 2),
        (r"a\\b", 3),  # \\ is one backslash
        (r"a\_b", 2.5),  # \_ is a half-width space
        ("日本語", 6),  # full-width characters count double
        ("éèàç", 4),
    ],
)
def test_text_width(line, width):
    assert text_width(line, ACTORS) == width


def _msg(*rows, code=10110):
    return [Command(code=code, string=rows[0].encode("cp1252"))] + [
        Command(code=20110, string=r.encode("cp1252")) for r in rows[1:]
    ]


def _face(name):
    return [Command(code=10130, string=name.encode(), params=[0, 0, 0])]


def test_width_with_and_without_face():
    ctx = Ctx(names={"actors": ACTORS})
    ok, long = "x" * 50, "x" * 51
    assert command_problems(_msg(ok, ok), ctx, MessageLimits()) == []
    (p,) = command_problems(_msg(ok, long), ctx, MessageLimits())
    assert "row 2 of message" in p and "51 characters wide (max 50 without a face)" in p
    cmds = _face("Actor1") + _msg("x" * 39) + _face("") + _msg("x" * 39)
    (p,) = command_problems(cmds, ctx, MessageLimits())
    assert "39 characters wide (max 38 with a face)" in p


def test_rows_and_choices():
    ctx = Ctx()
    (p,) = command_problems(_msg("a", "b", "c", "d", "e"), ctx, MessageLimits())
    assert "has 5 rows (max 4)" in p
    choice = [Command(code=10140, params=[0]), Command(code=20140, string=b"y" * 51)]
    (p,) = command_problems(choice, ctx, MessageLimits())
    assert p.startswith("choice ")
    assert command_problems(_msg("x" * 60), ctx, MessageLimits(width=60)) == []
