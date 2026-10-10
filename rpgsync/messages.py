"""Does every message fit in the message window?

RPG Maker 2000/2003 show 4 rows of about 50 half-width characters, 38 when
a face is shown.  Longer rows are cut off on screen, extra rows are lost.

Used by the example test of the scripts folder (tests/test_scripts.py).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel

from .commands import Ctx
from .lcf import Command


class MessageLimits(BaseModel):
    width: int = 50
    width_with_face: int = 38
    rows: int = 4
    variable_width: int = 3


# \c[n] colour, \s[n] speed, \$ gold window, \! \. \| \^ \> \< waits and flow
_INVISIBLE = re.compile(r"\\[cCsS]\[(?:\d+|\\[vV]\[\d+\])\]|\\[$!.|^><]")
_ACTOR = re.compile(r"\\[nN]\[(\d+|\\[vV]\[\d+\])\]")
_VARIABLE = re.compile(r"\\[vV]\[(\d+|\\[vV]\[\d+\])\]")


def text_width(line: str, actor_names: dict[int, str], variable_width: int = 3) -> float:
    """Width of a message row in half-width characters, as displayed."""
    longest_name = max((len(n) for n in actor_names.values()), default=6)

    def actor(m: re.Match) -> str:
        n = m.group(1)
        name = actor_names.get(int(n)) if n.isdigit() else None
        return name if name is not None else "x" * longest_name  # \n[0] (leader) or \n[\v[..]]: the longest name

    line = _INVISIBLE.sub("", line)
    line = _ACTOR.sub(actor, line)
    line = _VARIABLE.sub("0" * variable_width, line)
    line = line.replace("\\\\", "\\").replace("\\_", "\0")  # \\ is one backslash, \_ a half-width space
    width = 0.0
    for ch in line:
        if ch == "\0":
            width += 0.5
        elif unicodedata.east_asian_width(ch) in ("F", "W"):
            width += 2
        elif unicodedata.category(ch) not in ("Mn", "Cc"):
            width += 1
    return width


def _excerpt(text: str, n: int = 40) -> str:
    return '"%s"' % (text if len(text) <= n else text[: n - 3] + "...")


def command_problems(cmds: Sequence[Command], ctx: Ctx, limits: MessageLimits) -> list[str]:
    """Problems of the messages of one command list (a page or a common event).
    The face is followed in command order and assumed hidden at the start."""
    actors = ctx.names.get("actors", {})
    problems = []
    face = False
    i = 0
    while i < len(cmds):
        c = cmds[i]
        if c.code == 10130:
            face = bool(c.string)
        elif c.code in (10110, 20140):
            rows = [ctx.dec(c.string)]
            if c.code == 10110:
                while i + 1 < len(cmds) and cmds[i + 1].code == 20110:
                    i += 1
                    rows.append(ctx.dec(cmds[i].string))
                if len(rows) > limits.rows:
                    problems.append("message %s has %d rows (max %d)" % (_excerpt(rows[0]), len(rows), limits.rows))
            limit = limits.width_with_face if face else limits.width
            what = "message" if c.code == 10110 else "choice"
            for n, row in enumerate(rows, 1):
                width = text_width(row, actors, limits.variable_width)
                if width > limit:
                    where = "row %d of %s" % (n, what) if len(rows) > 1 else what
                    problems.append(
                        "%s %s is %g characters wide (max %d %s a face)"
                        % (where, _excerpt(row), width, limit, "with" if face else "without")
                    )
        i += 1
    return problems


def message_problems(specs: Sequence, ctx: Ctx, limits: MessageLimits | None = None) -> list[str]:
    """Problems of compiled map events (EventSpec), common events (CommonEventSpec)
    or troop battle events (TroopSpec)."""
    limits = limits or MessageLimits()
    out = []
    for spec in specs:
        if not hasattr(spec, "pages") and not hasattr(spec, "commands"):
            continue  # table size, database entries
        # a troop's name lives in database/troops.py: its spec only has the @troop(...) label
        troop = not hasattr(spec, "name")
        name = spec.label or "" if troop else spec.name
        name = name.decode(ctx.encoding, "replace") if isinstance(name, bytes) else name
        if hasattr(spec, "pages"):
            for n, page in enumerate(spec.pages, 1):
                where = "%s %d %s page %d" % ("troop" if troop else "event", spec.id, name, n)
                out += ["%s: %s" % (where, p) for p in command_problems(page.commands, ctx, limits)]
        elif hasattr(spec, "commands"):
            out += ["common event %d %s: %s" % (spec.id, name, p) for p in command_problems(spec.commands, ctx, limits)]
    return out
