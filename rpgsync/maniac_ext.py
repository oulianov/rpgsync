"""Maniac Patch extensions of standard commands, written as typed Python.

The Maniac Patch gives standard commands more options: values from variables
(4-bit selectors, see pyexpr.mval_src), names from string variables, more
loop kinds, events chosen by a variable...  The standard command keeps its
code and its usual parameters, followed by Maniac's.  This module reads and
writes the forms the other modules don't::

    for variables.i in range(10):            # Loop: 10 times, counter in variables.i
    for _ in range(1, variables.n + 1):      # Loop: count up
    while variables.hp > 0:                  # Loop: while
    continue                                 # Break Loop: next iteration
    common_events[variables.ce]()            # Call Event: common event from a variable
    events[variables.target].erase()         # Erase Event of another event
    events[3].restore()                      # ... bring an erased event back
    call_system_function("load")             # Call System Functions (Open Save Menu)
    disable_system_functions(save=True)      # Control System Functions (Change Save Access)

wait(), play_bgm(), face(), message_options() take their Maniac options as
keyword arguments (rpgsync.commands); Control Variables are in pyexpr.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from . import commands as K
from .commands import Call, CompileError, Ctx, pystr
from .lcf import Command

# --------------------------------------------------------------------------
# Values given as text, a string variable or strings[variables[n]]
# --------------------------------------------------------------------------


def text_src(mode: int, n: int, text: str) -> str | None:
    """A name or text the Maniac editor lets you give three ways (0 the command's text,
    1 strings[n], 2 strings[variables[n]])."""
    from . import pyexpr as P

    if mode == 0:
        return pystr(text) if n == 0 else None
    if text:
        return None
    if mode == 1:
        return P.str_src(n)
    if mode == 2:
        return "strings[%s]" % P.var_src(n)
    return None


def text_val(v, what: str, node) -> tuple[int, int, str]:
    """(mode, string variable, text) of a text_src() value."""
    from . import pyexpr as P

    if isinstance(v, str):
        return 0, 0, v
    if P.is_ref(v, "strings"):
        if P.is_int(v.index):
            return 1, v.index, ""
        if P.var_index(v.index) is not None:
            return 2, v.index.index, ""
    raise CompileError("%s is a string, strings[id] or strings[variables[id]]" % what, node)


def _vlrd_src(mode: int, v: int) -> str | None:
    """A value given as a number, variables[n] or variables[variables[n]]."""
    from . import pyexpr as P

    return P.mval_src(mode, v) if mode in (0, 1, 2) else None


def _vlrd_val(v, what: str) -> tuple[int, int]:
    from . import pyexpr as P

    return P.mval_val(v, what, modes=3)


# --------------------------------------------------------------------------
# Loop (12210 / 22210)
# --------------------------------------------------------------------------
#
# p0 kind: 0 forever, 1 X times, 2 count up, 3 count down, 4 while, 5 do-while (6-8:
# lists and map events, no Python form); p1 selectors of p2 and p3 (4 bits each), the
# comparison of while loops << 8; p2, p3 the values; p4 the variable counting the
# turns (0: none).  End Loop repeats the parameters.

LOOP_OPS = ["==", ">=", "<=", ">", "<", "!="]


def loop_src(p: list[int]) -> str | None:
    """The header of a loop (`while True`, `for variables[4] in range(10)`...) for
    Loop's parameters in RPG Maker's form, None when it has no Python form."""
    from . import pyexpr as P

    if not p:
        return "while True"
    if len(p) != 5 or p[0] not in (1, 2, 3, 4) or p[4] < 0 or not P._fits(p[1], 3):
        return None
    kind, m, a, b, index = p
    op = m >> 8
    a_src, b_src = _vlrd_src(m & 0xF, a), _vlrd_src((m >> 4) & 0xF, b)
    if a_src is None or b_src is None or op >= len(LOOP_OPS):
        return None
    target = P.var_src(index) if index else "_"
    if kind == 4:
        return None if index else "while %s %s %s" % (a_src, LOOP_OPS[op], b_src)
    if op:
        return None
    if kind == 1:
        return None if (m >> 4) & 0xF or b else "for %s in range(%s)" % (target, a_src)
    if kind == 2:
        stop = str(b + 1) if (m >> 4) & 0xF == 0 else "%s + 1" % b_src
        return "for %s in range(%s, %s)" % (target, a_src, stop)
    stop = str(b - 1) if (m >> 4) & 0xF == 0 else "%s - 1" % b_src
    return "for %s in range(%s, %s, -1)" % (target, a_src, stop)


def loop_val(stmt: ast.While | ast.For, ctx: Ctx) -> list[int]:
    """Loop's parameters (RPG Maker's form) for a `while` / `for` statement."""
    from . import pyexpr as P

    if stmt.orelse:
        raise CompileError("RPG Maker loops have no else:", stmt)
    if isinstance(stmt, ast.While):
        if isinstance(stmt.test, ast.Constant) and stmt.test.value is True:
            return []
        test = P.evaluate(stmt.test)
        if not isinstance(test, P.Cmp) or test.op not in LOOP_OPS:
            raise CompileError(
                "RPG Maker loops are `while True:`; with the Maniac Patch also `while a < b:` "
                "(a and b numbers, variables[id] or variables[variables[id]])",
                stmt,
            )
        ka, a = _vlrd_val(test.left, "the left side of the comparison")
        kb, b = _vlrd_val(test.right, "the right side of the comparison")
        return [4, ka | kb << 4 | LOOP_OPS.index(test.op) << 8, a, b, 0]
    if isinstance(stmt.target, ast.Name) and stmt.target.id == "_":
        index = 0
    else:
        index = P.var_index(P.evaluate(stmt.target))
        if index is None:
            raise CompileError("a Maniac loop counts in variables[id] (or _: no counter)", stmt.target)
    it = P.evaluate(stmt.iter)
    if not (
        isinstance(it, Call) and it.target is None and it.name == "range" and not it.kwargs and 1 <= len(it.args) <= 3
    ):
        raise CompileError(
            "Maniac loops go over range(count), range(first, last + 1) or range(first, last - 1, -1)", stmt.iter
        )
    args = list(it.args)
    if len(args) == 1:
        ka, a = _vlrd_val(args[0], "the number of turns")
        return [1, ka, a, 0, index]
    step = args[2] if len(args) == 3 else 1
    if step not in (1, -1):
        raise CompileError("Maniac loops count by 1 or -1", stmt.iter)
    ka, a = _vlrd_val(args[0], "the first value")
    stop = args[1]
    delta = 1 if step == 1 else -1
    if P.is_int(stop):
        kb, b = 0, stop - delta
    elif isinstance(stop, P.BinOp) and stop.op == ("+" if step == 1 else "-") and stop.right == 1:
        kb, b = _vlrd_val(stop.left, "the last value")
    else:
        raise CompileError(
            "the end of the range is a number, or variables[id] %s 1 (the last value is included)"
            % ("+" if step == 1 else "-"),
            stmt.iter,
        )
    return [2 if step == 1 else 3, ka | kb << 4, a, b, index]


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------

SYSTEM_FUNCTIONS = ["save", "load", "party", "screen_mode", "options", "debug", "license", "reset"]
SCREEN_MODES = ["toggle", "window", "fullscreen"]
# Control System Functions: forbidden functions (bit -> name)
SYSTEM_FLAGS = {0: "save", 2: "party", 3: "screen_toggle", 4: "options", 5: "debug", 7: "reset"}


# Scroll Map in pixels (11060, kinds 4-7): p1 selectors of x, y and the speed (4 bits each);
# p4 flags: 1 wait, 2 from the screen's centre, 4 from the current position (absolute
# pans); p5 the speed, or the duration in frames for the smooth pans (kinds 5, 7)
PAN_PIXELS = {
    4: ("pan_screen_by", "speed"),
    5: ("pan_screen_by", "frames"),
    6: ("pan_screen_to", "speed"),
    7: ("pan_screen_to", "frames"),
}
PAN_FLAGS = {1: "wait", 2: "centered", 4: "relative"}


def _pan_src(p: list[int]) -> str | None:
    from . import pyexpr as P

    if len(p) != 6 or p[0] not in PAN_PIXELS or not P._fits(p[1], 3) or p[4] & ~(7 if p[0] >= 6 else 1):
        return None
    name, unit = PAN_PIXELS[p[0]]
    values = [_vlrd_src((p[1] >> 4 * k) & 0xF, v) for k, v in enumerate((p[2], p[3], p[5]))]
    if None in values:
        return None
    kw = ["%s=%s" % (unit, values[2])] + ["%s=True" % n for bit, n in PAN_FLAGS.items() if p[4] & bit]
    return "%s(%s)" % (name, ", ".join(values[:2] + kw))


def _pan_val(call: Call) -> Command:
    by = call.name == "pan_screen_by"
    a = K.Args(call, ["x", "y", "speed", "frames", "wait"] + ([] if by else ["centered", "relative"]))
    if ("speed" in a.values) == ("frames" in a.values):
        raise CompileError(
            "%s(): give speed=... or frames=... (a smooth move in that many frames)" % call.name, call.node
        )
    unit = "speed" if "speed" in a.values else "frames"
    modes, values = 0, []
    for k, key in enumerate(("x", "y", unit)):
        mode, v = _vlrd_val(a.get(key, 0), key)
        modes |= mode << 4 * k
        values.append(v)
    kind = (4 if by else 6) + (unit == "frames")
    flags = sum(bit for bit, n in PAN_FLAGS.items() if n in a.values and a.bool(n))
    return Command(code=11060, params=[kind, modes, values[0], values[1], flags, values[2]])


def decode(c: Command, ctx: Ctx, in_loop: bool = False) -> Iterator[str]:
    """The Maniac forms of a standard command (each still to be checked)."""
    from . import pyexpr as P

    p = c.params
    if c.string:
        return
    if c.code == 11060:
        src = _pan_src(p)
        if src is not None:
            yield src
    elif c.code == 12330 and len(p) == 3 and p[0] in (3, 4):
        ce = "common_events[%s]" % _vlrd_src(p[0] - 2, p[1])
        yield "%s(%s)" % (ce, "page=%d" % p[2] if p[2] else "")
    elif c.code == 12320 and len(p) == 3 and p[0] in (0, 16, 32) and p[1] in (0, 1):
        verb = ("erase", "restore")[p[1]]
        if p[0] == 0 and p[2] in (0, 10005):
            if p[2] == 0:
                yield "this.%s()" % verb
            else:
                yield "this.%s(this_id=True)" % verb
        elif p[0] == 0:
            yield "%s.%s()" % (P.char_src(p[2]) if p[2] < 10001 else "events[%d]" % p[2], verb)
        else:
            yield "events[%s].%s()" % (_vlrd_src(p[0] >> 4, p[2]), verb)
    elif c.code == 12220 and len(p) == 2 and p[0] in (0, 1) and p[1] >= 0:
        word = ("break", "continue")[p[0]]
        if in_loop and p[1] == 0:
            yield word
        yield "%s_loop(%s)" % (word, "levels=%d" % (p[1] + 1) if p[1] else "")
    elif c.code == 11910 and len(p) == 2 and 0 <= p[0] < len(SYSTEM_FUNCTIONS):
        name = SYSTEM_FUNCTIONS[p[0]]
        if name == "screen_mode" and 0 <= p[1] < len(SCREEN_MODES):
            yield "call_system_function(%s, %s)" % (pystr(name), pystr(SCREEN_MODES[p[1]]))
        else:
            yield "call_system_function(%s%s)" % (pystr(name), ", %d" % p[1] if p[1] else "")
    elif c.code == 11930 and len(p) == 2 and p[0] == -1:
        mask = p[1]
        if mask >= 0 and not mask & ~sum(1 << b for b in SYSTEM_FLAGS):
            on = [n for b, n in SYSTEM_FLAGS.items() if mask >> b & 1]
            yield "disable_system_functions(%s)" % ", ".join("%s=True" % n for n in on)


def encode_call(call: Call, ctx: Ctx) -> Command | None:
    """A Maniac form of a standard command, None for any other call."""
    from . import pyexpr as P

    t = call.target

    def C(code, params):
        return Command(code=code, params=list(params))

    if t is None and call.name in ("pan_screen_by", "pan_screen_to"):
        return _pan_val(call)
    if t is None and call.name in ("break_loop", "continue_loop") and (call.args or call.kwargs):
        a = K.Args(call, ["levels"])
        levels = a.int("levels", 1)
        if levels < 1:
            raise CompileError("%s(): levels is 1 or more" % call.name, call.node)
        return C(12220, [0 if call.name == "break_loop" else 1, levels - 1])
    if t is None and call.name == "continue_loop":
        K.Args(call, [])
        return C(12220, [1, 0])
    if t is None and call.name == "call_system_function":
        a = K.Args(call, ["function", "option"])
        f = a.enum("function", SYSTEM_FUNCTIONS)
        opt = a.get("option", 0)
        if f == SYSTEM_FUNCTIONS.index("screen_mode") and isinstance(opt, str):
            opt = a.enum("option", SCREEN_MODES)
        elif not P.is_int(opt):
            raise CompileError("call_system_function(): option is a number", call.node)
        return C(11910, [f, opt])
    if t is None and call.name == "disable_system_functions":
        a = K.Args(call, list(SYSTEM_FLAGS.values()))
        mask = sum(1 << b for b, n in SYSTEM_FLAGS.items() if a.bool(n))
        return C(11930, [-1, mask])
    if ctx.dynparams and isinstance(t, P.Ref) and t.coll in ("common_events", "events") and not P.is_int(t.index):
        return None  # a game running DynParams gives the id its own way
    if call.name == "__call__" and P.is_ref(t, "common_events") and not P.is_int(t.index):
        a = K.Args(call, ["page"])
        mode, n = _vlrd_val(t.index, "the common event")
        return C(12330, [mode + 2, n, a.int("page", 0)])
    if call.name in ("erase", "restore") and t is not None:
        a = K.Args(call, ["this_id"])
        verb = 0 if call.name == "erase" else 1
        if isinstance(t, P.Sym) and t.name == "this":
            if call.name == "erase" and not a.bool("this_id"):
                return C(12320, []) if not a.values else C(12320, [0, 0, 0])
            return C(12320, [0, verb, 10005 if a.bool("this_id") else 0])
        K.Args(call, [])
        if P.is_ref(t, "events") and not P.is_int(t.index):
            mode, n = _vlrd_val(t.index, "the event")
            return C(12320, [mode << 4, verb, n])
        n = P.need_char(t, "an event")
        if n > 10005 or n in (10001, 10002, 10003, 10004) and not P.is_ref(t, "events"):
            raise CompileError("%s() works on map events" % call.name, call.node)
        return C(12320, [0, verb, n])
    return None


# --------------------------------------------------------------------------
# Engine check: Maniac options in a game without the patch
# --------------------------------------------------------------------------


def maniac_option(c: Command) -> str | None:
    """What Maniac-only option a standard command uses (None: none), for games that don't
    declare the patch: RPG_RT ignores the option or misreads the command."""
    from . import layout as L

    p = list((L.to_standard(c) or c).params) if c.code in L.FORMS else list(c.params)
    code = c.code
    if code == 10220 and len(p) >= 7:
        if p[0] >= 3 or p[3] >= 6 or p[4] >= 9 or any(p[7:]) or (p[0] == 1 and p[1] > p[2]):
            return "Control Variables operand, operation or target"
    elif code == 10210 and len(p) >= 4 and (p[0] >= 3 or (p[0] == 1 and p[1] > p[2])):
        return "Control Switches target"
    elif code in (12210, 22210) and any(p):
        return "loop kind (for / while)"
    elif code == 12220 and any(p):
        return "Break Loop levels / continue"
    elif code == 11410 and len(p) >= 2 and (p[1] & 256 or any(p[2:])):
        return "Wait in frames or by variable"
    elif code == 12330 and p and (p[0] >= 3 or any(p[3:])):
        return "Call Event by variable"
    elif code == 12320 and any(p):
        return "Erase Event of another event / restore"
    elif code == 11910 and any(p):
        return "Call System Functions"
    elif code == 11930 and p and p[0] == -1:
        return "Control System Functions"
    elif code == 10120 and any(p[4:]):
        return "message window size and font"
    elif code == 10130 and any(p[3:]):
        return "face from a string variable"
    elif code in (11510, 11550) and any(p[4 if code == 11510 else 3 :]):
        return "sound name or values from variables"
    elif code == 12010 and p and p[0] >= 12:
        return "Conditional Branch condition"
    return None
