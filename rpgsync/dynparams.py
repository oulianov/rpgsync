"""DynParams plugin: which parameter of which command a comment overwrites.

DynParams (DynPlugins/DynParams.dll) rewrites the parameters of the next
command from comment commands::

    @dynparams_add_param 2, V152    parameter 2 (1-based) := variable 152
    @dynparams_overwrite_next       apply to the next command

The decompiler names what they overwrite in a hint:
``dyn.dynparams_add_param(2, variables.id)  # → ConditionalBranch: switch``.
"""

from __future__ import annotations

from pydantic import BaseModel

from . import commands as K
from . import named
from .commands import Ctx
from .lcf import Command

# Lines the plugin never overwrites: the overwrite waits for the next command
SKIPPED = {0, K.END, *K.GROUPS}

TARGETS = {"pictures": "picture", "actors": "actor", "enemies": "enemy", "char": "event", "vehicle": "vehicle"}

# Parameters of the commands written by hand in pyexpr (not described in named.SPECS),
# from the DynParams readme. The last name repeats for the remaining parameters.
TABLE: dict[int, list[str]] = {
    10120: ["transparent", "position", "auto position", "continue events"],
    10130: ["face index", "right side", "flip"],
    10140: ["cancel"],
    10150: ["digits", "variable"],
    10210: ["target type", "first switch", "last switch", "operation"],
    10220: ["target type", "first variable", "last variable", "operation", "operand type", "operand", "operand 2"],
    10230: ["operation", "time type", "time", "visible", "in battle", "timer"],
    10310: ["operation", "amount type", "amount"],
    10320: ["operation", "item type", "item", "amount type", "amount"],
    10330: ["operation", "actor type", "actor"],
    10410: ["actor type", "actor", "operation", "amount type", "amount", "level-up message"],
    10420: ["actor type", "actor", "operation", "amount type", "amount", "level-up message"],
    10490: ["actor type", "actor"],
    10810: ["map", "x", "y", "direction"],
    10860: ["event", "position type", "x", "y", "direction"],
    10910: ["position type", "x", "y", "variable"],
    10920: ["position type", "x", "y", "variable"],
    11210: ["animation", "target", "wait", "whole map"],
    11330: ["event", "frequency", "repeat", "skippable", "route step"],
    11410: ["duration", "wait for key"],
    11510: ["fade in", "volume", "tempo", "balance"],
    11550: ["volume", "tempo", "balance"],
    11610: [
        "variable",
        "wait",
        "unused",
        "decision",
        "cancel",
        "numbers",
        "operators",
        "time variable",
        "store time",
        "shift",
        "down",
        "left",
        "right",
        "up",
    ],
}
CONDITION = ["condition type", "first reference", "second type", "second reference", "third reference", "else branch"]
BY_TYPE: dict[tuple[int, int], list[str]] = {
    (12010, 0): ["condition type", "switch", "on/off", "unused", "unused", "else branch"],
    (12010, 1): ["condition type", "variable", "operand type", "operand", "comparison", "else branch"],
    (12330, 0): ["call type", "common event", "unused"],
    (12330, 1): ["call type", "event", "page"],
    (12330, 2): ["call type", "event variable", "page variable"],
}


def _field_slots(f: named.Field) -> list[str]:
    name = f.name.lstrip("_")
    if f.kind == "bits":
        return []
    if f.kind == "const":
        return ["mode"]
    if f.kind == "xy":
        return ["position type", "x", "y"]
    if f.kind == "mxy":
        return ["position type", "map", "x", "y"]
    if f.kind in ("actor", "value", "amount3"):
        return [name + " type", name]
    if f.kind in ("optid", "optvar"):
        return ["use " + name, name]
    return [name]


def slots(c: Command, ctx: Ctx) -> list[str]:
    """Name of each parameter of a command (its last name repeats for the parameters after)."""
    p = c.params
    if p and (c.code, p[0]) in BY_TYPE:
        return BY_TYPE[(c.code, p[0])]
    if c.code in (12010, 13310):
        return CONDITION
    if c.code in TABLE:
        return TABLE[c.code]
    specs = named.BY_CODE.get(c.code, [])
    spec = next((s for s in specs if s.decode(c, ctx) is not None), specs[0] if specs else None)
    if spec is None:
        return []
    names = [TARGETS.get(spec.target, spec.target)] if spec.target else []
    for f in spec.fields:
        names += _field_slots(f)
    return names


def slot_hint(c: Command, index: int, ctx: Ctx) -> str:
    """``ConditionalBranch: switch`` for parameter 2 (1-based) of an if."""
    what = K.CODE_NAMES.get(c.code, str(c.code))
    if not 1 <= index <= len(c.params):
        return "%s has no parameter %d (it has %d)" % (what, index, len(c.params))
    names = slots(c, ctx)
    if not names:
        return "%s: parameter %d" % (what, index)
    return "%s: %s" % (what, names[min(index, len(names)) - 1])


def _call(cmds: list[Command], i: int) -> tuple[str, list[str]] | None:
    """(function, arguments) of a DynParams comment command, else None."""
    if cmds[i].code != 12410 or not cmds[i].string[:11].lower() == b"@dynparams_":
        return None
    text = cmds[i].string
    for c in cmds[i + 1 :]:  # DynRPG reads the following comment lines too
        if c.code != 22410:
            break
        text += c.string
    head, _, rest = text.decode("latin-1").partition(" ")
    args = [a.strip() for a in rest.split(",")] if rest.strip() else []
    return head[1:].lower(), args


class Overwrite(BaseModel):
    """What the comments build until @dynparams_overwrite_next, and the command it rewrites
    (positions in the command list)."""

    at: int | None = None  # the @dynparams_overwrite_next comment, None if not reached
    target: int | None = None  # the rewritten command, None when not among the commands
    params: list[tuple[int, int]] = []  # (comment, 1-based parameter index)
    lines: list[tuple[int, int]] = []  # (comment, message line)
    choices: list[tuple[int, int]] = []  # (comment, choice)


def _int(arg: str) -> int | None:
    return int(arg) if arg.lstrip("-").isdigit() else None


def play(cmds: list[Command]) -> list[Overwrite]:
    """Plays the plugin on sibling commands (one block, in order): one Overwrite per
    @dynparams_overwrite_next, plus the last one when it is not applied among them."""
    out: list[Overwrite] = []
    cur = Overwrite()
    index: int | None = 5  # @dynparams_add_param with one value: parameter 5, then 6, ...
    for i, c in enumerate(cmds):
        if cur.at is not None and c.code not in SKIPPED and c.code != 22410:
            cur.target = i
            out.append(cur)
            cur, index = Overwrite(), 5
            continue  # its text may be rewritten: don't read it as a command
        call = _call(cmds, i)
        if call is None:
            continue
        func, args = call
        n = _int(args[0]) if args else None
        if func == "dynparams_add_param":
            if len(args) == 1:
                if index is not None:
                    cur.params.append((i, index))
                    index += 1
            elif n is not None:
                cur.params.append((i, n))
        elif func == "dynparams_set_index":
            index = n  # None: set from a variable, unknown
        elif func in ("dynparams_message_line_append_string", "dynparams_message_line_append_number"):
            if n is not None:
                cur.lines.append((i, n))
        elif func in ("dynparams_choice_case_append_string", "dynparams_choice_case_append_number"):
            if n is not None:
                cur.choices.append((i, n))
        elif func == "dynparams_clear_params":
            cur, index = Overwrite(), 5
        elif func == "dynparams_overwrite_next":
            cur.at = i
    if cur.at is not None:
        out.append(cur)
    return out


def hints(cmds: list[Command], ctx: Ctx) -> dict[int, str]:
    """Hints for the DynParams comments among sibling commands: the parameter each
    @dynparams_add_param overwrites, the command @dynparams_overwrite_next rewrites.
    A target outside these commands (after the end of the block) gets no hint."""
    out: dict[int, str] = {}
    for o in play(cmds):
        if o.target is None:
            continue
        target = cmds[o.target]
        for i, index in o.params:
            out[i] = "→ " + slot_hint(target, index, ctx)
        out[o.at] = "→ " + K.CODE_NAMES.get(target.code, str(target.code))
    return out


# --------------------------------------------------------------------------
# Checks: errors break the game in RPG_RT, warnings do not
# --------------------------------------------------------------------------

FUNCTIONS = {
    "dynparams_add_param",
    "dynparams_set_index",
    "dynparams_append_string",
    "dynparams_append_number",
    "dynparams_message_line_append_string",
    "dynparams_message_line_append_number",
    "dynparams_choice_case_append_string",
    "dynparams_choice_case_append_number",
    "dynparams_overwrite_next",
    "dynparams_clear_params",
    "dynparams_list_params",
    "dynparams_start_record",
    "dynparams_stop_record",
}
BUILDERS = FUNCTIONS - {
    "dynparams_overwrite_next",
    "dynparams_clear_params",
    "dynparams_list_params",
    "dynparams_start_record",
    "dynparams_stop_record",
}
MESSAGE_LINES = 4


class Problem(BaseModel):
    error: bool  # breaks the game in RPG_RT (crash, error box); else a warning
    message: str


def _blocks(cmds: list[Command]) -> list[list[int]]:
    """Positions of the sibling commands of each block (same indent, same parent)."""
    blocks: list[list[int]] = []
    stack: list[tuple[int, list[int]]] = []
    for i, c in enumerate(cmds):
        while stack and stack[-1][0] > c.indent:
            stack.pop()
        if not stack or stack[-1][0] < c.indent:
            stack.append((c.indent, []))
            blocks.append(stack[-1][1])
        stack[-1][1].append(i)
    return blocks


def _loop_of(cmds: list[Command], i: int) -> int | None:
    """The loop command around command i, else None."""
    indent = cmds[i].indent
    for j in range(i - 1, -1, -1):
        if cmds[j].indent < indent:
            if cmds[j].code == 12210:
                return j
            indent = cmds[j].indent
    return None


def _name(c: Command) -> str:
    return K.CODE_NAMES.get(c.code, str(c.code))


def problems(cmds: list[Command]) -> list[Problem]:
    """DynParams mistakes in an event's command list."""
    out: dict[str, Problem] = {}

    def add(error: bool, i: int, what: str) -> None:
        text = cmds[i].string.decode("latin-1")
        message = "%s: %s" % (text, what)
        out.setdefault(message, Problem(error=error, message=message))

    # each comment on its own
    unapplied = None
    for i in range(len(cmds)):
        call = _call(cmds, i)
        if call is None:
            continue
        func, args = call
        n = _int(args[0]) if args else None
        if func not in FUNCTIONS:
            add(False, i, "unknown DynParams command, RPG_RT ignores it")
        elif func == "dynparams_add_param" and len(args) > 1 and n is not None and n < 1:
            add(True, i, "parameters start at 1: DynParams writes outside the command (RPG_RT may crash)")
        elif func.startswith("dynparams_message_line_") and n is not None and not 1 <= n <= MESSAGE_LINES:
            add(True, i, "message lines are 1 to %d: DynParams shows an error box" % MESSAGE_LINES)
        elif func.startswith("dynparams_choice_case_") and n is not None and n < 1:
            add(True, i, "choices start at 1: DynParams shows an error box")
        if func in BUILDERS and unapplied is None:
            # applied later, or at the next turn of the loop around it
            loop = _loop_of(cmds, i)
            start = i + 1 if loop is None else loop + 1
            others = (_call(cmds, j) for j in range(start, len(cmds)) if j != i)
            if not any(c and c[0] in ("dynparams_overwrite_next", "dynparams_clear_params") for c in others):
                unapplied = i
    if unapplied is not None:
        add(False, unapplied, "never applied: no @dynparams_overwrite_next after it")

    # what each overwrite does to its command
    for block in _blocks(cmds):
        siblings = [cmds[i] for i in block]
        for o in play(siblings):
            if o.target is None:
                add(False, block[o.at], "no command after it in its block: it rewrites whatever runs next")
                continue
            target = siblings[o.target]
            for i, index in o.params:
                if not 1 <= index <= len(target.params):
                    add(
                        True,
                        block[i],
                        "%s has no parameter %d (it has %d): DynParams writes outside the command "
                        "(RPG_RT may crash)" % (_name(target), index, len(target.params)),
                    )
            j = o.target + 1
            while j < len(siblings) and siblings[j].code == 20110:  # the message's other lines
                j += 1
            if o.lines:
                if target.code != 10110:
                    add(False, block[o.lines[0][0]], "message lines only change a message, not %s" % _name(target))
                else:
                    shown = j - o.target
                    for i, line in o.lines:
                        if line > shown:
                            add(False, block[i], "the message has %d line(s): line %d is not shown" % (shown, line))
            if o.choices:
                k = o.target if target.code == 10140 else j if target.code == 10110 else None
                if k is None or k >= len(siblings) or siblings[k].code != 10140:
                    add(
                        False,
                        block[o.choices[0][0]],
                        "choices only change a Show Choices (or one right after the message), not %s" % _name(target),
                    )
                    continue
                count = 0
                for c in siblings[k + 1 :]:
                    if c.code == 20141:
                        break
                    count += c.code == 20140
                for i, choice in o.choices:
                    if choice > count:
                        add(
                            False, block[i], "Show Choices has %d choice(s): choice %d is not changed" % (count, choice)
                        )
    return list(out.values())
