"""EasyRPG Player's own event commands, written ``easyrpg.name(...)`` like the Maniac Patch's.

    easyrpg.trigger_event_at(x=variables.x, y=variables.y)
    easyrpg.pathfinder(event=player, x=19, y=20, diagonal=True, frequency=8)
    easyrpg.wait_for_single_movement(event=player, result=variables.arrived)
    easyrpg.set_interpreter_flag(patch="maniac", on=True)
    easyrpg.process_json(op="get", json=strings.save, path="/gold", target=variables.gold)
    easyrpg.clone_map_event(map=maps.lab, event=3, x=7, y=2, name="Copy")
    easyrpg.destroy_map_event(event=events[variables.target])

EasyRPG Player runs them when its extensions are on (``EasyRPG=1`` in the [Patch]
section of EasyRPG.ini).  Each value takes two parameters, (mode, value): 0 a number,
1 ``variables[n]``, 2 ``variables[variables[n]]`` (Player's ValueOrVariable; the
pointer needs the Maniac Patch).  Where the value is the id of a variable, switch or
string variable the command reads or writes, the argument names that one:
``variables[5]``, ``variables[variables[5]]`` (the pair (1, 5): its id is in
variables[5]).

The named forms take keyword arguments only.  Any EasyRPG command can also be written
by name with its raw parameters as positional numbers, ``easyrpg.animate_variable(0, 1,
text="...")``: rpgsync writes that form when a command's parameters don't fit its named
form, and for the commands liblcf knows that EasyRPG Player doesn't run (2050, 2052,
2058).  Other codes are ``easyrpg.command_NNNN(...)``.  Both forms compile back to the
same bytes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from . import commands as K
from . import pyexpr as P
from .commands import Call, CompileError, Ctx, pystr
from .lcf import Command

NAMESPACE = "easyrpg"

# codes from liblcf (generator/csv/enums_easyrpg.csv)
NAMES = {
    2002: "trigger_event_at",
    2003: "pathfinder",
    2050: "call_movement_action",
    2051: "wait_for_single_movement",
    2052: "animate_variable",
    2053: "set_interpreter_flag",
    2055: "process_json",
    2056: "clone_map_event",
    2057: "destroy_map_event",
    2058: "string_picture_menu",
}
CODES = range(2000, 3000)


def name_of(code: int) -> str:
    return NAMES.get(code, "command_%d" % code)


def code_of(name: str) -> int | None:
    for code, n in NAMES.items():
        if n == name:
            return code
    if name.startswith("command_") and name[8:].isdigit() and int(name[8:]) in CODES:
        return int(name[8:])
    return None


# --------------------------------------------------------------------------
# Values: (mode, value) pairs
# --------------------------------------------------------------------------

VALUE_MODES = 3  # 0 a number, 1 variables[n], 2 variables[variables[n]]
_COLLECTION_SRC = {"variables": P.var_src, "switches": P.sw_src, "strings": P.str_src}


def value_src(mode: int, v: int) -> str | None:
    """A number, variables[n] or variables[variables[n]]; None for another mode."""
    return P.mval_src(mode, v) if 0 <= mode < VALUE_MODES else None


def value_val(v, what: str) -> tuple[int, int]:
    """(mode, value) of a value_src() value."""
    return P.mval_val(v, what, modes=VALUE_MODES)


def event_src(mode: int, v: int) -> str | None:
    """An event given by a pair: this, player, events[3], an event of the map by name,
    events[variables[n]] or events[variables[variables[n]]]."""
    if mode == 0:
        return P.char_src(v)
    src = value_src(mode, v)
    return None if src is None else "events[%s]" % src


def event_val(v, what: str) -> tuple[int, int]:
    if P.is_ref(v, "events") and not P.is_int(v.index):
        mode, n = value_val(v.index, what)
        return mode, n
    cid = P.char_id(v)
    if cid is None:
        raise P._err(
            "%s is this, player, boat, ship, airship, events[id], events[variables[id]] or an event "
            "of this map (got %s)" % (what, P.show(v)),
            v,
        )
    return 0, cid


def ref_src(coll: str, mode: int, n: int) -> str | None:
    """A variable, switch or string variable whose id a pair gives: coll[n],
    coll[variables[n]] or coll[variables[variables[n]]]."""
    if mode == 0:
        return _COLLECTION_SRC[coll](n)
    src = value_src(mode, n)
    return None if src is None else "%s[%s]" % (coll, src)


def ref_val(v, coll: str) -> tuple[int, int] | None:
    """The pair of a ref_src() value, None when it is no such reference."""
    if not P.is_ref(v, coll):
        return None
    if P.is_int(v.index):
        return 0, v.index
    try:
        return value_val(v.index, coll)
    except CompileError:
        return None


def _flags_src(flags: int, names: dict[int, str]) -> list[str] | None:
    """`name=True` for each bit set; None when a bit has no name."""
    if flags < 0 or flags & ~sum(names):
        return None
    return ["%s=True" % n for bit, n in names.items() if flags & bit]


def _flags_val(a: K.Args, names: dict[int, str]) -> int:
    return sum(bit for bit, n in names.items() if a.bool(n))


def _extra_src(tail: list[int]) -> list[str]:
    if not tail:
        return []
    return ["extra=(%s%s)" % (", ".join(str(x) for x in tail), "," if len(tail) == 1 else "")]


def _extra_val(a: K.Args, call: Call) -> list[int]:
    extra = a.get("extra")
    if extra is None:
        return []
    if not (isinstance(extra, tuple) and all(P.is_int(x) for x in extra)):
        raise CompileError("%s(): extra must be a tuple of integers" % call.name, call.node)
    return list(extra)


def _call_src(code: int, kwargs: list[str]) -> str:
    return "%s.%s(%s)" % (NAMESPACE, NAMES[code], ", ".join(kwargs))


# --------------------------------------------------------------------------
# Trigger Event At (2002)
# --------------------------------------------------------------------------
#
#     easyrpg.trigger_event_at(x=variables.x, y=variables.y, face_player=False)
#
# p0-p3 the cell (x and y pairs); p4 flags: 1 the event turns to the hero.  The first
# version of the command has no p4 (the event turns to the hero): face_player left
# out keeps that form.


def _decode_trigger(c: Command, ctx: Ctx) -> str | None:
    p = c.params
    if c.string or len(p) < 4:
        return None
    x, y = value_src(p[0], p[1]), value_src(p[2], p[3])
    if x is None or y is None:
        return None
    kw = ["x=%s" % x, "y=%s" % y]
    if len(p) > 4:
        if p[4] not in (0, 1):
            return None
        kw += ["face_player=%s" % bool(p[4])] + _extra_src(p[5:])
    return _call_src(2002, kw)


def _encode_trigger(call: Call, ctx: Ctx) -> Command:
    a = K.Args(call, ARGS[2002])
    params = [*value_val(a.req("x"), "trigger_event_at(): x"), *value_val(a.req("y"), "trigger_event_at(): y")]
    if "face_player" in a.values:
        params.append(int(a.bool("face_player")))
    elif "extra" in a.values:
        raise CompileError("trigger_event_at(): extra=... comes after face_player=...", call.node)
    return Command(code=2002, params=params + _extra_val(a, call))


# --------------------------------------------------------------------------
# Pathfinder (2003)
# --------------------------------------------------------------------------
#
#     easyrpg.pathfinder(event=player, x=19, y=20, diagonal=True, ignore=[haru], frequency=8)
#
# p0-p9 pairs: the event, x, y, the search's iteration limit, the route's steps (-1:
# all); p10 flags: 1 wait while the event moves, 2 diagonal moves, 4 debug log, 8 skip
# the command while the event moves, 16 skip the steps that fail; p11 0: p12 events
# ignored, their ids in p13...; p11 1-2: (p11, p12) is a pair giving a variable that
# holds how many, the next variables their ids.  Then the move frequency pair, absent
# in older commands (EasyRPG Player then uses 3).

PATHFINDER_FLAGS = {2: "diagonal", 16: "skippable", 4: "debug"}
WHILE_MOVING = {0: "replace", 1: "wait", 8: "skip"}
PATHFINDER_DEFAULTS = {"search_limit": (0, 500), "max_steps": (0, -1)}


def _pathfinder_tail(p: list[int]) -> int | None:
    """Where the move frequency pair starts (after the events ignored), None when the
    list of ignored events is cut short."""
    if len(p) < 13:
        return None
    if p[11] != 0:
        return 13
    n = p[12]
    return 13 + n if n >= 0 and 13 + n <= len(p) else None


def _decode_pathfinder(c: Command, ctx: Ctx) -> str | None:
    p = c.params
    tail = _pathfinder_tail(p)
    if c.string or tail is None:
        return None
    event = event_src(p[0], p[1])
    x, y, search, steps = (value_src(p[k], p[k + 1]) for k in (2, 4, 6, 8))
    flags = p[10]
    moving = WHILE_MOVING.get(flags & 9)
    named = _flags_src(flags & ~9, PATHFINDER_FLAGS)
    if p[11] == 0:
        ignore = "[%s]" % ", ".join(P.char_src(n) for n in p[13:tail]) if tail > 13 else ""
    else:
        ignore = ref_src("variables", p[11], p[12])
    if None in (event, x, y, search, steps, ignore) or moving is None or named is None or flags < 0:
        return None
    kw = ["event=%s" % event, "x=%s" % x, "y=%s" % y]
    if (p[6], p[7]) != PATHFINDER_DEFAULTS["search_limit"]:
        kw.append("search_limit=%s" % search)
    if (p[8], p[9]) != PATHFINDER_DEFAULTS["max_steps"]:
        kw.append("max_steps=%s" % steps)
    if moving != "replace":
        kw.append("while_moving=%s" % pystr(moving))
    kw += named
    if ignore:
        kw.append("ignore=%s" % ignore)
    rest = p[tail:]
    if len(rest) == 1:
        return None
    if rest:
        frequency = value_src(rest[0], rest[1])
        if frequency is None:
            return None
        kw += ["frequency=%s" % frequency] + _extra_src(rest[2:])
    return _call_src(2003, kw)


def _encode_pathfinder(call: Call, ctx: Ctx) -> Command:
    a = K.Args(call, ARGS[2003])
    params = list(event_val(a.req("event"), "pathfinder(): event"))
    params += value_val(a.req("x"), "pathfinder(): x") + value_val(a.req("y"), "pathfinder(): y")
    for key, default in PATHFINDER_DEFAULTS.items():
        params += value_val(a.get(key), "pathfinder(): " + key) if key in a.values else default
    moving = a.get("while_moving", "replace")
    codes = {n: bit for bit, n in WHILE_MOVING.items()}
    if moving not in codes:
        raise CompileError('pathfinder(): while_moving is "replace", "wait" or "skip"', call.node)
    params.append(codes[moving] | _flags_val(a, PATHFINDER_FLAGS))
    ignore = a.get("ignore", [])
    if isinstance(ignore, (list, tuple)):
        ids = [event_val(v, "pathfinder(): an event ignored") for v in ignore]
        if any(mode for mode, _ in ids):
            raise CompileError("pathfinder(): the events ignored are listed by id, not by variable", call.node)
        params += [0, len(ids)] + [n for _, n in ids]
    else:
        pair = ref_val(ignore, "variables")
        if pair is None or pair[0] == 0:
            raise CompileError(
                "pathfinder(): ignore is a list of events ([events[3], haru]), or variables[variables[id]]: "
                "the variable whose id is in variables[id] holds how many, the next ones their ids",
                call.node,
            )
        params += pair
    if "frequency" in a.values:
        params += value_val(a.get("frequency"), "pathfinder(): frequency")
    elif "extra" in a.values:
        raise CompileError("pathfinder(): extra=... comes after frequency=...", call.node)
    return Command(code=2003, params=params + _extra_val(a, call))


# --------------------------------------------------------------------------
# Wait for Single Movement (2051)
# --------------------------------------------------------------------------
#
#     easyrpg.wait_for_single_movement(event=haru, max_failures=10, result=variables.arrived)
#
# p0-p5 pairs: the event; the failed moves allowed (0: wait until the route ends); the
# id of the variable that gets 1 when the route ends, 0 when it is given up (0: none).


def _decode_wait(c: Command, ctx: Ctx) -> str | None:
    p = c.params
    if c.string or len(p) < 6:
        return None
    event, failures = event_src(p[0], p[1]), value_src(p[2], p[3])
    no_result = (p[4], p[5]) == (0, 0)
    result = None if no_result else ref_src("variables", p[4], p[5])
    if event is None or failures is None or (result is None and not no_result):
        return None
    kw = ["event=%s" % event]
    if (p[2], p[3]) != (0, 0):
        kw.append("max_failures=%s" % failures)
    if result is not None:
        kw.append("result=%s" % result)
    return _call_src(2051, kw + _extra_src(p[6:]))


def _encode_wait(call: Call, ctx: Ctx) -> Command:
    a = K.Args(call, ARGS[2051])
    params = list(event_val(a.req("event"), "wait_for_single_movement(): event"))
    params += value_val(a.get("max_failures", 0), "wait_for_single_movement(): max_failures")
    result = a.get("result")
    pair = (0, 0) if result is None else ref_val(result, "variables")
    if pair is None:
        raise CompileError(
            "wait_for_single_movement(): result is None, variables[id] or variables[variables[id]]", call.node
        )
    return Command(code=2051, params=params + list(pair) + _extra_val(a, call))


# --------------------------------------------------------------------------
# Set Interpreter Flag (2053)
# --------------------------------------------------------------------------
#
#     easyrpg.set_interpreter_flag(patch="pic-unlock", on=variables.unlocked)
#
# The string names the patch (any case for EasyRPG Player; the named form keeps the
# names as listed here); p0, p1 the pair: 0 off, anything else on.  With no name,
# p2 is the patch's number (1 destiny ... 8 rpg2k-battle).  The patch is on or off for
# the event running the command and the events it calls, until it ends.

INTERPRETER_FLAGS = [
    "destiny",
    "dynrpg",
    "maniac",
    "common-this",
    "pic-unlock",
    "key-patch",
    "rpg2k3-cmds",
    "rpg2k3-commands",
    "rpg2k-battle",
]
INTERPRETER_FLAG_NUMBERS = range(1, 9)


def _decode_flag(c: Command, ctx: Ctx) -> str | None:
    p = c.params
    if len(p) < 2:
        return None
    on = {0: "False", 1: "True"}.get(p[1], str(p[1])) if p[0] == 0 else value_src(p[0], p[1])
    name = ctx.dec(c.string)
    if name in INTERPRETER_FLAGS:
        patch, tail = pystr(name), p[2:]
    elif not name and len(p) > 2 and p[2] in INTERPRETER_FLAG_NUMBERS:
        patch, tail = str(p[2]), p[3:]
    else:
        return None
    if on is None:
        return None
    return _call_src(2053, ["patch=%s" % patch, "on=%s" % on] + _extra_src(tail))


def _encode_flag(call: Call, ctx: Ctx) -> Command:
    a = K.Args(call, ARGS[2053])
    on = a.req("on")
    params = [0, int(on)] if isinstance(on, bool) else list(value_val(on, "set_interpreter_flag(): on"))
    patch = a.req("patch")
    if isinstance(patch, str) and patch in INTERPRETER_FLAGS:
        string = ctx.enc(patch, call.node)
    elif P.is_int(patch) and patch in INTERPRETER_FLAG_NUMBERS:
        string, params = b"", params + [patch]
    else:
        raise CompileError(
            "set_interpreter_flag(): patch is one of %s, or its number (1-8)"
            % ", ".join(pystr(n) for n in INTERPRETER_FLAGS),
            call.node,
        )
    return Command(code=2053, params=params + _extra_val(a, call), string=string)


# --------------------------------------------------------------------------
# Process JSON (2055)
# --------------------------------------------------------------------------
#
#     easyrpg.process_json(op="get", json=strings.save, path="/gold", target=variables.gold)
#     easyrpg.process_json(op="get_keys", json='{"a": 1}', target=strings[1], keys_object=True)
#
# p0, p1 the operation pair; p2 where the JSON is: 0 the command's string (p3: its
# length), 1 strings[p3], 2 strings[variables[p3]], 3 strings[variables[variables[p3]]];
# p4, p5 the target's kind pair (0 switch, 1 variable, 2 string variable), p6, p7 its id
# pair; p8, p9 the path, as p2, p3 (1 and 2 only); p10 flags: 1 replace the codes of
# the path (\V[n]...), 2 the same with hexadecimal numbers (1 wins), 4 pretty print (a
# string target), 8 Get Keys wraps the keys in {"keys": [...]}.  When the command holds
# both, its string is the path, then the JSON (EasyRPG Player reads the path first);
# lengths count characters (Unicode code points).

JSON_OPERATIONS = ["get", "set", "get_length", "get_keys", "get_type", "remove", "push", "pop", "contains"]
JSON_TARGETS = ["switches", "variables", "strings"]
JSON_FLAGS = {1: "expand", 2: "hex", 4: "pretty", 8: "keys_object"}


def _decode_json(c: Command, ctx: Ctx) -> str | None:
    p = c.params
    if len(p) < 11:
        return None
    if p[0] == 0:
        op = pystr(JSON_OPERATIONS[p[1]]) if 0 <= p[1] < len(JSON_OPERATIONS) else None
    else:
        op = value_src(p[0], p[1])
    text, pos = ctx.dec(c.string), 0
    if p[8] == 0:
        if not 0 <= p[9] <= len(text):
            return None
        path, pos = pystr(text[: p[9]]), p[9]
    else:
        path = ref_src("strings", p[8] - 1, p[9]) if p[8] in (1, 2) else None
    if p[2] == 0:
        if not 0 <= p[3] <= len(text) - pos:
            return None
        json, pos = pystr(text[pos : pos + p[3]]), pos + p[3]
    else:
        json = ref_src("strings", p[2] - 1, p[3]) if p[2] in (1, 2, 3) else None
    if pos != len(text):
        return None
    if p[4:8] == [0, 0, 0, 0]:
        target = ""
    elif p[4] == 0 and 0 <= p[5] < len(JSON_TARGETS):
        target = ref_src(JSON_TARGETS[p[5]], p[6], p[7])
    else:
        target = None
    flags = _flags_src(p[10], JSON_FLAGS)
    if op is None or path is None or json is None or target is None or flags is None:
        return None
    kw = ["op=%s" % op, "json=%s" % json]
    if path != '""':
        kw.append("path=%s" % path)
    if target:
        kw.append("target=%s" % target)
    return _call_src(2055, kw + flags + _extra_src(p[11:]))


def _json_text(v, what: str, modes: int, node) -> tuple[str, int, int]:
    """(the text the command holds, mode, id) of a JSON or a path: a literal, or
    strings[...] (modes: how many forms of the id it takes)."""
    if isinstance(v, str):
        return v, 0, len(v)
    pair = ref_val(v, "strings")
    if pair is None or pair[0] >= modes:
        forms = ["strings[id]", "strings[variables[id]]", "strings[variables[variables[id]]]"][:modes]
        raise CompileError("%s is a text, %s" % (what, " or ".join(forms)), node)
    return "", pair[0] + 1, pair[1]


def _encode_json(call: Call, ctx: Ctx) -> Command:
    a = K.Args(call, ARGS[2055])
    op = a.req("op")
    if isinstance(op, str) and op in JSON_OPERATIONS:
        params = [0, JSON_OPERATIONS.index(op)]
    elif not isinstance(op, (str, int)):
        params = list(value_val(op, "process_json(): op"))
    else:
        raise CompileError(
            "process_json(): op is one of %s, or variables[id]" % ", ".join(pystr(n) for n in JSON_OPERATIONS),
            call.node,
        )
    json, json_mode, json_id = _json_text(a.req("json"), "process_json(): json", 3, call.node)
    path, path_mode, path_id = _json_text(a.get("path", ""), "process_json(): path", 2, call.node)
    target = a.get("target")
    found = None if target is not None else (0, 0, 0)
    for kind, coll in enumerate(JSON_TARGETS):
        pair = ref_val(target, coll) if found is None else None
        if pair is not None:
            found = (kind, *pair)
    if found is None:
        raise CompileError(
            "process_json(): target is switches[...], variables[...] or strings[...] "
            "(an id, variables[id] or variables[variables[id]])",
            call.node,
        )
    params += [json_mode, json_id, 0, found[0], found[1], found[2], path_mode, path_id, _flags_val(a, JSON_FLAGS)]
    return Command(code=2055, params=params + _extra_val(a, call), string=ctx.enc(path + json, call.node))


# --------------------------------------------------------------------------
# Clone Map Event (2056)
# --------------------------------------------------------------------------
#
#     easyrpg.clone_map_event(map=maps.lab, event=3, x=7, y=2, name="Copy")
#
# p0-p9 pairs: the source map (0: this one), the source event's id, x, y, the new
# event's id (0 or less: the next free one); p10 where the name is: 0 the command's
# string (empty: the source's name), 1 strings[p11], 2 strings[variables[p11]].


def _decode_clone(c: Command, ctx: Ctx) -> str | None:
    from . import maniac_ext as X

    p = c.params
    if len(p) < 12:
        return None
    if p[0] == 0:
        source_map = "" if p[1] == 0 else P.map_src(p[1])
    else:
        source_map = value_src(p[0], p[1])
    event, x, y, new_id = (value_src(p[k], p[k + 1]) for k in (2, 4, 6, 8))
    name = X.text_src(p[10], p[11], ctx.dec(c.string))
    if None in (source_map, event, x, y, new_id, name):
        return None
    kw = ["map=%s" % source_map] if source_map else []
    kw += ["event=%s" % event, "x=%s" % x, "y=%s" % y]
    if (p[8], p[9]) != (0, 0):
        kw.append("new_id=%s" % new_id)
    if name != '""':
        kw.append("name=%s" % name)
    return _call_src(2056, kw + _extra_src(p[12:]))


def _encode_clone(call: Call, ctx: Ctx) -> Command:
    from . import maniac_ext as X

    a = K.Args(call, ARGS[2056])
    params = list(value_val(a.get("map", 0), "clone_map_event(): map"))
    for key in ("event", "x", "y"):
        params += value_val(a.req(key), "clone_map_event(): " + key)
    params += value_val(a.get("new_id", 0), "clone_map_event(): new_id")
    mode, n, name = X.text_val(a.get("name", ""), "clone_map_event(): name", call.node)
    return Command(code=2056, params=params + [mode, n] + _extra_val(a, call), string=ctx.enc(name, call.node))


# --------------------------------------------------------------------------
# Destroy Map Event (2057)
# --------------------------------------------------------------------------
#
#     easyrpg.destroy_map_event(event=events[variables.target])
#
# p0, p1 the event's pair; 0 (the default) and 10005 are the event running the command.


def _decode_destroy(c: Command, ctx: Ctx) -> str | None:
    p = c.params
    if c.string or len(p) < 2:
        return None
    kw = []
    if (p[0], p[1]) != (0, 0):
        event = event_src(p[0], p[1])
        if event is None:
            return None
        kw.append("event=%s" % event)
    return _call_src(2057, kw + _extra_src(p[2:]))


def _encode_destroy(call: Call, ctx: Ctx) -> Command:
    a = K.Args(call, ARGS[2057])
    pair = event_val(a.get("event"), "destroy_map_event(): event") if "event" in a.values else (0, 0)
    return Command(code=2057, params=list(pair) + _extra_val(a, call))


# the keyword arguments of each named form (rpgsync.dsl.EasyRPG has the same, typed)
ARGS = {
    2002: ["x", "y", "face_player", "extra"],
    2003: ["event", "x", "y", "search_limit", "max_steps", "while_moving", *PATHFINDER_FLAGS.values()]
    + ["ignore", "frequency", "extra"],
    2051: ["event", "max_failures", "result", "extra"],
    2053: ["patch", "on", "extra"],
    2055: ["op", "json", "path", "target", *JSON_FLAGS.values(), "extra"],
    2056: ["map", "event", "x", "y", "new_id", "name", "extra"],
    2057: ["event", "extra"],
}

Decoder = Callable[[Command, Ctx], str | None]
Encoder = Callable[[Call, Ctx], Command]

DECODERS: dict[int, Decoder] = {
    2002: _decode_trigger,
    2003: _decode_pathfinder,
    2051: _decode_wait,
    2053: _decode_flag,
    2055: _decode_json,
    2056: _decode_clone,
    2057: _decode_destroy,
}
ENCODERS: dict[str, Encoder] = {
    NAMES[2002]: _encode_trigger,
    NAMES[2003]: _encode_pathfinder,
    NAMES[2051]: _encode_wait,
    NAMES[2053]: _encode_flag,
    NAMES[2055]: _encode_json,
    NAMES[2056]: _encode_clone,
    NAMES[2057]: _encode_destroy,
}


# --------------------------------------------------------------------------
# Decoding and encoding
# --------------------------------------------------------------------------


def decode_all(c: Command, ctx: Ctx) -> Iterator[str]:
    """The forms of an EasyRPG command, best first (each still to be checked): its named
    form, then its raw parameters."""
    if c.code not in CODES:
        return
    dec = DECODERS.get(c.code)
    src = dec(c, ctx) if dec else None
    if src is not None:
        yield src
    src = decode_generic(c, ctx)
    if src is not None:
        yield src


def decode(c: Command, ctx: Ctx) -> str | None:
    return next(decode_all(c, ctx), None)


def decode_generic(c: Command, ctx: Ctx) -> str | None:
    """Any EasyRPG command: ``easyrpg.name(p0, p1, ..., text="...")``."""
    if c.code not in CODES:
        return None
    if not c.params and not c.string and c.code in DECODERS:
        return None  # would read as the named form with its defaults
    args = [str(p) for p in c.params]
    if c.string:
        args.append("text=%s" % pystr(ctx.dec(c.string)))
    return "%s.%s(%s)" % (NAMESPACE, name_of(c.code), ", ".join(args))


def encode(call: Call, ctx: Ctx) -> Command:
    """``easyrpg.name(...)``: the named form for keyword arguments, the raw parameters for
    positional numbers (and text=...)."""
    enc = ENCODERS.get(call.name)
    if enc is not None and not call.args and "text" not in call.kwargs:
        return enc(call, ctx)
    code = code_of(call.name)
    if code is None:
        raise CompileError("unknown EasyRPG command %s.%s()" % (NAMESPACE, call.name), call.node)
    for p in call.args:
        if not P.is_int(p):
            raise CompileError(
                "%s.%s(): without names, the parameters are numbers (the command's raw parameters), got %s"
                % (NAMESPACE, call.name, P.show(p)),
                call.node,
            )
    unknown = set(call.kwargs) - {"text"}
    if unknown:
        raise CompileError(
            "%s.%s() got %s: write its raw parameters as numbers, and text=..."
            % (NAMESPACE, call.name, ", ".join(sorted(unknown))),
            call.node,
        )
    text = call.kwargs.get("text", "")
    if not isinstance(text, str):
        raise CompileError("%s.%s(): text must be a string" % (NAMESPACE, call.name), call.node)
    return Command(code=code, params=list(call.args), string=ctx.enc(text, call.node))


# --------------------------------------------------------------------------
# Engine checks
# --------------------------------------------------------------------------

# parameters starting a (mode, value) pair, per command (Pathfinder's frequency: after
# the events ignored)
_PAIRS = {
    2002: (0, 2),
    2003: (0, 2, 4, 6, 8),
    2051: (0, 2, 4),
    2053: (0,),
    2055: (0, 4, 6),
    2056: (0, 2, 4, 6, 8),
    2057: (0,),
}


def maniac_option(c: Command) -> str | None:
    """What part of an EasyRPG command needs the Maniac Patch in EasyRPG Player (None:
    none): without it, a value through a pointer reads as -1, a string variable target
    and a name from a string variable are ignored."""
    p = c.params
    starts = list(_PAIRS.get(c.code, ()))
    if c.code == 2003:
        tail = _pathfinder_tail(p)
        starts += [11] + ([tail] if tail is not None else [])
    if c.code == 2055 and len(p) > 3 and p[2] == 3:
        return "the JSON in strings[variables[variables[n]]]"
    if any(k + 1 < len(p) and p[k] >= 2 for k in starts):
        return "a value through variables[variables[n]]"
    if c.code == 2055 and len(p) > 5 and p[4] == 0 and p[5] == 2:
        return "a string variable as target"
    if c.code == 2056 and len(p) > 11 and p[10] in (1, 2):
        return "the name from a string variable"
    return None


# Call System Functions (11910, the Maniac Patch's): EasyRPG Player opens its scenes
SYSTEM_FUNCTIONS = {200: "main_menu", 201: "items", 205: "order", 206: "settings", 207: "language"}
SYSTEM_FUNCTION_IDS = range(200, 210)  # 202-204 (skills, equipment, status) take a hero: raw parameters


def easyrpg_option(c: Command) -> str | None:
    """What EasyRPG Player option a standard command uses (None: none)."""
    if c.code == 11910 and c.params and c.params[0] in SYSTEM_FUNCTION_IDS:
        return "Call System Functions %d: a scene of EasyRPG Player's menu" % c.params[0]
    return None
