"""Pythonic expressions and statements of event scripts.

This module maps game objects to Python syntax:

    variables[1] = 9999999                 # Control Variables
    variables[2] += variables[3]
    switches[5] = True                     # Control Switches
    switches[1:11].toggle()
    party.gold += 100                      # Change Gold
    items[3].count -= 1                    # Change Items
    party.add(actors[2])                   # Change Party Members
    actors[1].name = "Alex"                # Change Hero Name
    this.move(move_up, move_left)          # Move Event
    events[5].set_location(3, 4)           # Set Event Location
    common_events[4]()                     # Call Event
    if variables[1] >= variables[2]: ...   # Conditional Branch

Ranges use Python slices, so ``variables[1:11]`` is variables 1..10.
"""

from __future__ import annotations

import ast
import keyword
import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from . import commands as K
from . import dynrpg
from .commands import Call, Command, CompileError, Ctx, Sym, pystr

# --------------------------------------------------------------------------
# Expression model
# --------------------------------------------------------------------------

COLLECTIONS = (
    "variables",
    "switches",
    "items",
    "actors",
    "events",
    "common_events",
    "enemies",
    "pictures",
    "strings",
    "party",  # party[0]: the first member (Maniac Patch)
)
CHARACTERS = {10001: "player", 10002: "boat", 10003: "ship", 10004: "airship", 10005: "this"}
CHARACTER_IDS = {v: k for k, v in CHARACTERS.items()}

COMPARE_OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<="}
COMPARE = ["==", ">=", "<=", ">", "<", "!="]  # RPG Maker operator order
AUG_OPS = {
    ast.Add: "+=",
    ast.Sub: "-=",
    ast.Mult: "*=",
    ast.FloorDiv: "//=",
    ast.Mod: "%=",
    ast.BitOr: "|=",  # |= and the ones after it: Maniac Patch
    ast.BitAnd: "&=",
    ast.BitXor: "^=",
    ast.LShift: "<<=",
    ast.RShift: ">>=",
}
VAR_OPS = ["=", "+=", "-=", "*=", "//=", "%=", "|=", "&=", "^=", "<<=", ">>="]
# a op b in a value (Maniac Patch Control Variables, operand "a op b"): op 1-10
BIN_OPS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.BitOr: "|",
    ast.BitAnd: "&",
    ast.BitXor: "^",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.Pow: "**",
}


class Ref(BaseModel):
    """``variables[1]``, ``switches[variables[2]]``, ``items[1:5]``…"""

    coll: str
    index: Any
    node: Any = None


class Range(BaseModel):
    start: Any
    stop: Any
    step: int = 1  # -1: from start down to stop + 1 (the Maniac Patch keeps the order)


class Attr(BaseModel):
    obj: Any
    name: str
    node: Any = None


class Cmp(BaseModel):
    left: Any
    op: str
    right: Any
    node: Any = None


class In(BaseModel):
    left: Any
    right: Any
    negated: bool = False
    node: Any = None


class Not(BaseModel):
    value: Any
    node: Any = None


class Neg(BaseModel):
    value: Any


class Pos(BaseModel):
    value: Any


class And(BaseModel):
    values: list[Any]
    node: Any = None


class BinOp(BaseModel):
    """``a + b``, ``a & 255``... (Maniac Patch values)."""

    left: Any
    op: str
    right: Any
    node: Any = None


class IfExp(BaseModel):
    """``a if variables[1] > 0 else b`` (Maniac Patch values)."""

    test: Any
    body: Any
    orelse: Any
    node: Any = None


class Repeat(BaseModel):
    """``move_up * 3`` in a move route."""

    value: Any
    count: int
    node: Any = None


class DynToken(BaseModel):
    """DynRPG variable/actor token: ``V[12]`` is written ``V12`` in the comment."""

    prefix: str
    number: int


DYN_PREFIX = re.compile(r"^[Nn]?[Vv]*$")


def evaluate(node: ast.AST):
    """Turn an expression AST into plain values and the model above."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise CompileError("unsupported constant %r" % (node.value,), node)
    if isinstance(node, ast.UnaryOp):
        v = evaluate(node.operand)
        if isinstance(node.op, ast.Not):
            return Not(value=v, node=node)
        number = isinstance(v, (int, float)) and not isinstance(v, bool)
        if isinstance(node.op, ast.USub):
            return -v if number else Neg(value=v)
        if isinstance(node.op, ast.UAdd):
            return v if number else Pos(value=v)
    if isinstance(node, ast.Tuple):
        return tuple(evaluate(e) for e in node.elts)
    if isinstance(node, ast.List):
        return [evaluate(e) for e in node.elts]
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        return Sym(name=node.id, node=node)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id not in COLLECTIONS
        and DYN_PREFIX.match(node.value.id)
    ):
        n = evaluate(node.slice)
        if not is_int(n) or n < 0:
            raise CompileError("DynRPG tokens are written V[12], VV[3], N[1]", node)
        return DynToken(prefix=node.value.id, number=n)
    if isinstance(node, ast.Subscript):
        if not (isinstance(node.value, ast.Name) and node.value.id in COLLECTIONS):
            raise CompileError("indexing is only supported on %s" % ", ".join(COLLECTIONS), node)
        s = node.slice
        if isinstance(s, ast.Slice):
            step = evaluate(s.step) if s.step is not None else 1
            if step not in (1, -1) or s.lower is None or s.upper is None:
                raise CompileError(
                    "ranges are written [first:last+1], e.g. variables[1:11] (or [first:last-1:-1] backwards)", node
                )
            index = Range(start=evaluate(s.lower), stop=evaluate(s.upper), step=step)
        elif isinstance(s, ast.BinOp) and node.value.id in ("variables", "switches"):
            raise CompileError(
                "RPG Maker can't compute an id inside []: compute it in a variable first, "
                "e.g. `variables.ptr += 1` then %s[variables.ptr]" % node.value.id,
                node,
            )
        else:
            index = evaluate(s)
        return Ref(coll=node.value.id, index=index, node=node)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id in ("variables", "switches", "common_events", "strings"):
            return _named_ref(node)
        if isinstance(node.value, ast.Name) and node.value.id == "maps":
            return _map_id(node)
        return Attr(obj=evaluate(node.value), name=node.attr, node=node)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise CompileError("chained comparisons are not supported", node)
        left, right, op = evaluate(node.left), evaluate(node.comparators[0]), node.ops[0]
        if isinstance(op, (ast.In, ast.NotIn)):
            return In(left=left, right=right, negated=isinstance(op, ast.NotIn), node=node)
        if type(op) not in COMPARE_OPS:
            raise CompileError("unsupported comparison", node)
        return Cmp(left=left, op=COMPARE_OPS[type(op)], right=right, node=node)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or):
            raise CompileError("RPG Maker conditions cannot use 'or'; use elif or separate branches", node)
        values = []
        for v in node.values:
            ev = evaluate(v)
            values.extend(ev.values if isinstance(ev, And) else [ev])
        return And(values=values, node=node)
    if isinstance(node, ast.Call):
        args = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                raise CompileError("*args is not supported", node)
            args.append(evaluate(a))
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise CompileError("**kwargs is not supported", node)
            kwargs[kw.arg] = evaluate(kw.value)
        if isinstance(node.func, ast.Name):
            return Call(name=node.func.id, args=args, kwargs=kwargs, node=node)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "common_events"
        ):  # common_events.tombee_de_la_nuit() = common_events[12]()
            return Call(name="__call__", target=_named_ref(node.func), args=args, kwargs=kwargs, node=node)
        if isinstance(node.func, ast.Attribute):
            return Call(name=node.func.attr, target=evaluate(node.func.value), args=args, kwargs=kwargs, node=node)
        if isinstance(node.func, ast.Subscript):
            return Call(name="__call__", target=evaluate(node.func), args=args, kwargs=kwargs, node=node)
        raise CompileError("unsupported call", node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = evaluate(node.left), evaluate(node.right)
        if isinstance(a, str) and isinstance(b, str):
            return a + b
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        a, b = evaluate(node.left), evaluate(node.right)
        if isinstance(a, (Sym, Call)) and is_int(b) and b >= 1:
            return Repeat(value=a, count=b, node=node)
    if isinstance(node, ast.BinOp) and type(node.op) in BIN_OPS:
        a, b = evaluate(node.left), evaluate(node.right)
        if isinstance(a, str) or isinstance(b, str):
            raise CompileError("unsupported expression: %s" % ast.unparse(node), node)
        return BinOp(left=a, op=BIN_OPS[type(node.op)], right=b, node=node)
    if isinstance(node, ast.IfExp):
        return IfExp(test=evaluate(node.test), body=evaluate(node.body), orelse=evaluate(node.orelse), node=node)
    raise CompileError("unsupported expression: %s" % ast.unparse(node), node)


def show(v) -> str:
    """Source text of an evaluated value, for error messages."""
    node = getattr(v, "node", None)
    if node is not None:
        return ast.unparse(node)
    return repr(v)


def _err(msg, v=None, node=None):
    return CompileError(msg, node if node is not None else getattr(v, "node", None))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def is_ref(v, coll: str) -> bool:
    return isinstance(v, Ref) and v.coll == coll


def _pointer_ref(v):
    """``variables[variables.x]`` / ``switches[variables[3]]``: an access through a pointer, else None."""
    if isinstance(v, Ref) and v.coll in ("variables", "switches") and is_ref(v.index, "variables"):
        return v
    return None


def int_index(v, coll: str) -> int | None:
    """``coll[5]`` -> 5"""
    if is_ref(v, coll) and isinstance(v.index, int) and not isinstance(v.index, bool):
        return v.index
    return None


def var_index(v) -> int | None:
    return int_index(v, "variables")


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# Events of the map being converted, referred to by their function name:
# ``haru.move(...)`` instead of ``events[10].move(...)``.
_NONE: Mapping = MappingProxyType({})
_EVENT_NAMES: ContextVar[Mapping[int, str]] = ContextVar("event_names", default=_NONE)  # id -> name (decompile)
_EVENT_IDS: ContextVar[Mapping[str, int | None]] = ContextVar("event_ids", default=_NONE)  # name -> id (compile)
# event name -> its page functions: name -> page number (compile: gary.accusation() is Call Event page 3)
_EVENT_PAGES: ContextVar[Mapping[str, Mapping[str, int]]] = ContextVar("event_pages", default=_NONE)
_EVENT_PAGE_COUNTS: ContextVar[Mapping[int, int]] = ContextVar(
    "event_page_counts", default=_NONE
)  # id -> pages (decompile)


@contextmanager
def map_events(
    names: dict[int, str] | None = None,
    ids: dict[str, int | None] | None = None,
    pages: dict[str, dict[str, int]] | None = None,
    page_counts: dict[int, int] | None = None,
):
    """Make the events of one map file available by name while converting it."""
    t1, t2 = _EVENT_NAMES.set(names or {}), _EVENT_IDS.set(ids or {})
    t3, t4 = _EVENT_PAGES.set(pages or {}), _EVENT_PAGE_COUNTS.set(page_counts or {})
    try:
        yield
    finally:
        _EVENT_NAMES.reset(t1)
        _EVENT_IDS.reset(t2)
        _EVENT_PAGES.reset(t3)
        _EVENT_PAGE_COUNTS.reset(t4)


# The event whose pages are being converted: (class name, id); ``self`` in its pages
_SELF_EVENT: ContextVar[tuple[str, int | None] | None] = ContextVar("self_event", default=None)


@contextmanager
def current_event(name: str, eid: int | None):
    """Convert the pages of one event: ``self`` is that event."""
    token = _SELF_EVENT.set((name, eid))
    try:
        yield
    finally:
        _SELF_EVENT.reset(token)


def _event_of(t) -> str | None:
    """The class name of the map event a Sym names (``self``: the current event)."""
    if not isinstance(t, Sym) or t.name in CHARACTER_IDS:
        return None
    if t.name == "self":
        cur = _SELF_EVENT.get()
        return cur[0] if cur else None
    return t.name if t.name in _EVENT_IDS.get() else None


_PAGE_NAME = re.compile(r"page_(\d+)")


def page_number(event: str, name: str, node=None) -> int | None:
    """``gary.page_3()``: the number of the page `name` of the map event `event` (its
    position in the script, page_N otherwise), None when `name` is not a page."""
    pages = _EVENT_PAGES.get().get(event)
    if pages is not None and name in pages:
        return pages[name]
    m = _PAGE_NAME.fullmatch(name)
    if m is None:
        return None
    if pages is not None:
        raise CompileError(
            "%s has no %s (its pages: %s); %s.call(%s) runs a page by its number"
            % (event, name, ", ".join(pages) or "none", event, m.group(1)),
            node,
        )
    return int(m.group(1))


_DB_NAMES: ContextVar[Mapping[str, Mapping[int, str]]] = ContextVar("db_names", default=_NONE)


@contextmanager
def db_names(ctx: Ctx):
    """Refer to the database's named variables and switches by name
    (``variables.code_voulu``) while converting one file."""
    token = _DB_NAMES.set(ctx.handles)
    selectors = _MANIAC_SELECTORS.set(not ctx.dynparams)
    try:
        yield
    finally:
        _DB_NAMES.reset(token)
        _MANIAC_SELECTORS.reset(selectors)


# Maniac Patch ids given by a variable (actors[variables.n].hp...); not in a game running
# DynParams, which writes them its own way
_MANIAC_SELECTORS: ContextVar[bool] = ContextVar("maniac_selectors", default=True)


def var_src(n: int) -> str:
    name = _DB_NAMES.get().get("variables", _NONE).get(n)
    return "variables.%s" % name if name else "variables[%d]" % n


def ce_src(n: int) -> str:
    name = _DB_NAMES.get().get("common_events", _NONE).get(n)
    return "common_events.%s" % name if name else "common_events[%d]" % n


def sw_src(n: int) -> str:
    name = _DB_NAMES.get().get("switches", _NONE).get(n)
    return "switches.%s" % name if name else "switches[%d]" % n


def str_src(n: int) -> str:
    """A string variable (Maniac Patch): ``strings.name`` / ``strings[3]``."""
    name = _DB_NAMES.get().get("strings", _NONE).get(n)
    return "strings.%s" % name if name else "strings[%d]" % n


def map_src(n: int) -> str:
    """A map id as written in scripts: ``maps.relm`` (its attribute in database/map_tree.py)."""
    name = _DB_NAMES.get().get("maps", _NONE).get(n)
    return "maps.%s" % name if name else str(n)


_IDS: dict[int, tuple[Mapping[int, str], dict[str, int]]] = {}  # id(names) -> (names, name -> id)


def _ids(coll: str) -> dict[str, int]:
    """Name -> id (the first one) of the variables, switches, maps...: built once per
    set of names (a game has thousands, each script refers to many)."""
    names = _DB_NAMES.get().get(coll, _NONE)
    found = _IDS.get(id(names))
    if found is None or found[0] is not names:
        ids: dict[str, int] = {}
        for n, name in names.items():
            ids.setdefault(name, n)
        if len(_IDS) > 64:
            _IDS.clear()
        _IDS[id(names)] = found = (names, ids)
    return found[1]


def _map_id(node: ast.Attribute) -> int:
    """``maps.relm`` -> the id of that map"""
    n = _ids("maps").get(node.attr)
    if n is not None:
        return n
    raise CompileError("no map named %r in database/map_tree.py (deleted, or renamed there?)" % node.attr, node)


def _named_ref(node: ast.Attribute) -> Ref:
    """``variables.code_voulu`` -> Ref(variables, 32)"""
    coll = node.value.id  # type: ignore[attr-defined]
    n = _ids(coll).get(node.attr)
    if n is not None:
        return Ref(coll=coll, index=n, node=node)
    what = {"variables": "variable", "switches": "switch", "common_events": "common event", "strings": "string"}[coll]
    raise CompileError("no %s named %r in database/%s.py" % (what, node.attr, coll), node)


def event_name(cid: int) -> str | None:
    """Function name of a map event when converting its map, else None."""
    return _EVENT_NAMES.get().get(cid)


def char_src(cid: int) -> str:
    cur = _SELF_EVENT.get()
    if cur is not None and cur[1] == cid:
        return "self"
    return CHARACTERS.get(cid) or event_name(cid) or "events[%d]" % cid


def char_id(v) -> int | None:
    """``this`` / ``player`` / ``events[5]`` / ``haru`` (an event of this map) -> character id"""
    if isinstance(v, Sym):
        if v.name in CHARACTER_IDS:
            return CHARACTER_IDS[v.name]
        cur = _SELF_EVENT.get()
        if v.name == "self" and cur is not None:
            if cur[1] is None:
                raise _err("event %s has no id yet: give it one in @event(...) to use self" % cur[0], v)
            return cur[1]
        ids = _EVENT_IDS.get()
        if v.name in ids:
            if ids[v.name] is None:
                raise _err("event %s has no id yet: give it one in @event(...) to refer to it" % v.name, v)
            return ids[v.name]
    return int_index(v, "events")


def need_char(v, what="a character") -> int:
    cid = char_id(v)
    if cid is None:
        raise _err(
            "expected %s: this, player, boat, ship, airship, events[id] or an event of this map (got %s)"
            % (what, show(v)),
            v,
        )
    return cid


def value_src(kind: int, v: int) -> str | None:
    """number-or-variable operand used by many commands"""
    if kind == 0:
        return str(v)
    if kind == 1:
        return var_src(v)
    return None


def value_val(v, what="value") -> tuple[int, int]:
    if is_int(v):
        return 0, v
    n = var_index(v)
    if n is not None:
        return 1, n
    raise _err("%s must be a number or variables[id] (got %s)" % (what, show(v)), v)


# --------------------------------------------------------------------------
# Targets of Control Switches / Control Variables
# --------------------------------------------------------------------------


def target_src(coll: str, mode: int, a: int, b: int) -> str | None:
    if mode == 0 and a == b:
        return (
            {"variables": var_src, "switches": sw_src}[coll](a)
            if coll in ("variables", "switches")
            else "%s[%d]"
            % (
                coll,
                a,
            )
        )
    if mode == 1 and b >= a:
        return "%s[%d:%d]" % (coll, a, b + 1)
    if mode == 1:  # backwards (the Maniac Patch keeps the order)
        return "%s[%d:%d:-1]" % (coll, a, b - 1)
    if mode == 2 and b == 0:
        return "%s[%s]" % (coll, var_src(a))
    if mode == 3 and coll == "variables":  # Maniac Patch: from the variable a variable names, to another's
        return "%s[%s:%s + 1]" % (coll, var_src(a), var_src(b))
    return None


def target_val(v: Ref) -> tuple[int, int, int]:
    idx = v.index
    if is_int(idx):
        return 0, idx, idx
    if isinstance(idx, Range) and is_int(idx.start) and is_int(idx.stop):
        if idx.step == -1:
            if idx.stop >= idx.start:
                raise _err("empty range %s (backwards ranges are [first:last-1:-1])" % show(v), v)
            return 1, idx.start, idx.stop + 1
        if idx.stop <= idx.start:
            raise _err("empty range %s (ranges are [first:last+1])" % show(v), v)
        return 1, idx.start, idx.stop - 1
    n = var_index(idx)
    if n is not None:
        return 2, n, 0
    if (
        isinstance(idx, Range)
        and var_index(idx.start) is not None
        and isinstance(idx.stop, BinOp)
        and idx.stop.op == "+"
        and var_index(idx.stop.left) is not None
        and idx.stop.right == 1
    ):
        return 3, idx.start.index, idx.stop.left.index  # variables[variables.a : variables.b + 1] (Maniac Patch)
    raise _err("%s: index must be a number, a range [a:b] or variables[id]" % show(v), v)


# --------------------------------------------------------------------------
# Operands of Control Variables
# --------------------------------------------------------------------------

ITEM_PROPS = ["count", "equipped"]
ACTOR_PROPS = [
    "level",
    "exp",
    "hp",
    "sp",
    "max_hp",
    "max_sp",
    "attack",
    "defense",
    "spirit",
    "agility",
    "weapon",
    "shield",
    "armor",
    "helmet",
    "accessory",
]
EVENT_PROPS = ["map_id", "x", "y", "direction", "screen_x", "screen_y"]
ENEMY_PROPS = ["hp", "sp", "max_hp", "max_sp", "attack", "defense", "spirit", "agility"]
OTHER_SRC = {
    0: ("party", "gold"),
    1: ("timer1", "seconds"),
    2: ("party", "size"),
    3: ("game", "save_count"),
    4: ("game", "battle_count"),
    5: ("game", "win_count"),
    6: ("game", "defeat_count"),
    7: ("game", "escape_count"),
    8: ("game", "midi_ticks"),
    9: ("timer2", "seconds"),
}
OTHER_IDS = {v: k for k, v in OTHER_SRC.items()}


def operand_src(kind: int, p5: int, p6: int) -> str | None:
    if kind == 0:
        return str(p5) if p6 == 0 else None
    if kind == 1:
        return var_src(p5) if p6 == 0 else None
    if kind == 2:
        return "variables[%s]" % var_src(p5) if p6 == 0 else None
    if kind == 3:
        return "randint(%d, %d)" % (p5, p6)
    if kind == 4 and 0 <= p6 < len(ITEM_PROPS):
        return "items[%d].%s" % (p5, ITEM_PROPS[p6])
    if kind == 5 and 0 <= p6 < len(ACTOR_PROPS):
        return "actors[%d].%s" % (p5, ACTOR_PROPS[p6])
    if kind == 6 and 0 <= p6 < len(EVENT_PROPS):
        return "%s.%s" % (char_src(p5), EVENT_PROPS[p6])
    if kind == 7 and p6 == 0 and p5 in OTHER_SRC:
        return "%s.%s" % OTHER_SRC[p5]
    if kind == 8 and 0 <= p6 < len(ENEMY_PROPS):
        return "enemies[%d].%s" % (p5, ENEMY_PROPS[p6])
    return None


def operand_val(v) -> tuple[int, int, int]:
    if isinstance(v, bool):
        v = int(v)
    if is_int(v):
        return 0, v, 0
    if is_ref(v, "variables"):
        n = var_index(v)
        if n is not None:
            return 1, n, 0
        m = var_index(v.index)
        if m is not None:
            return 2, m, 0
    if isinstance(v, Call) and v.name == "randint" and v.target is None:
        a = K.Args(v, ["a", "b"])
        return 3, a.int("a"), a.int("b")
    if isinstance(v, Attr):
        o = v.obj
        if int_index(o, "items") is not None and v.name in ITEM_PROPS:
            return 4, o.index, ITEM_PROPS.index(v.name)
        if int_index(o, "actors") is not None and v.name in ACTOR_PROPS:
            return 5, o.index, ACTOR_PROPS.index(v.name)
        if char_id(o) is not None and v.name in EVENT_PROPS:
            return 6, char_id(o), EVENT_PROPS.index(v.name)
        if isinstance(o, Sym) and (o.name, v.name) in OTHER_IDS:
            return 7, OTHER_IDS[(o.name, v.name)], 0
        if int_index(o, "enemies") is not None and v.name in ENEMY_PROPS:
            return 8, o.index, ENEMY_PROPS.index(v.name)
    raise _err(
        "unsupported value %s. Use a number, variables[id], variables[variables[id]], randint(a, b), "
        "items[id].count, actors[id].level, this.x, party.gold, timer1.seconds, game.save_count, "
        "enemies[i].hp, ..." % show(v),
        v,
    )


# --------------------------------------------------------------------------
# Maniac Patch values and the Maniac operands of Control Variables
# --------------------------------------------------------------------------
#
# Where the Maniac Patch takes a value, a selector says how it is given (4 bits
# per value, packed in one parameter): 0 a number, 1 variables[n], 2
# variables[variables[n]], 3 switches[n] (1 when ON), 4 switches[variables[n]].

VALUE_FORMS = ("a number", "variables[id]", "variables[variables[id]]", "switches[id]", "switches[variables[id]]")


def mval_src(mode: int, v: int) -> str | None:
    """A Maniac value as written in scripts, None for an unknown selector."""
    if mode == 0:
        return str(v)
    if mode == 1:
        return var_src(v)
    if mode == 2:
        return "variables[%s]" % var_src(v)
    if mode == 3:
        return sw_src(v)
    if mode == 4:
        return "switches[%s]" % var_src(v)
    return None


def mval_val(v, what: str = "value", modes: int = 5) -> tuple[int, int]:
    """(selector, parameter) of a Maniac value; `modes`: how many of the forms it takes."""
    if is_int(v):
        return 0, v
    n = var_index(v)
    if n is not None:
        return 1, n
    if is_ref(v, "variables") and var_index(v.index) is not None:
        return 2, v.index.index
    if int_index(v, "switches") is not None:
        out = (3, v.index)
    elif is_ref(v, "switches") and var_index(v.index) is not None:
        out = (4, v.index.index)
    else:
        out = None
    if out is None or out[0] >= modes:
        raise _err("%s is %s (got %s)" % (what, " or ".join(VALUE_FORMS[:modes]), show(v)), v)
    return out


def _mv(m: int, slot: int, v: int) -> str | None:
    """The value of a selector slot of a packed selector parameter."""
    return mval_src((m >> 4 * slot) & 0xF, v)


def _packed(*modes: int) -> int:
    return sum(mode << 4 * slot for slot, mode in enumerate(modes))


def _fits(m: int, slots: int) -> bool:
    """Every bit of a packed selector parameter belongs to one of its first `slots` slots."""
    return m >= 0 and m >> 4 * slots == 0


MANIAC_ACTOR_PROPS = ACTOR_PROPS + ["id", "atb", "next_exp"]  # 15-17: Maniac Patch
MANIAC_EVENT_PROPS = EVENT_PROPS + ["id"]  # 6: the event's id (Maniac Patch)
MANIAC_ENEMY_PROPS = ENEMY_PROPS + ["id", "atb"]  # 8-9: Maniac Patch
MANIAC_OTHER = {10: ("game", "date"), 11: ("game", "time"), 12: ("game", "frames"), 13: ("game", "maniac_version")}
CV_BINARY = ["", "+", "-", "*", "//", "%", "|", "&", "^", "<<", ">>"]  # operand 19: op 1-10
CV_FUNCTIONS = {12: "sqrt", 13: "sin", 14: "cos", 15: "atan2", 16: "min", 17: "max", 18: "abs", 22: "lerp"}
CV_ARRAYS = {23: "array_sum", 24: "array_min", 25: "array_max"}
# Control Variables operands with the id of what they read and the property, then (Maniac
# Patch) how that id is given: collection, properties
CV_SELECTED = {
    4: ("items", ITEM_PROPS),
    5: ("actors", MANIAC_ACTOR_PROPS),
    6: ("events", MANIAC_EVENT_PROPS),
    8: ("enemies", MANIAC_ENEMY_PROPS),
    9: ("party", MANIAC_ACTOR_PROPS),
}


def _selected_src(kind: int, ident: int, prop: int, m: int) -> str | None:
    coll, props = CV_SELECTED[kind]
    if not 0 <= prop < len(props):
        return None
    if kind == 6 and m == 0:
        return "%s.%s" % (char_src(ident), props[prop])
    if m == 0:
        return "%s[%d].%s" % (coll, ident, props[prop])
    if kind == 6 and m in (1, 2):
        idx = mval_src(m, ident)
        return None if idx is None else "events[%s].%s" % (idx, props[prop])
    idx = mval_src(m, ident)
    return None if idx is None else "%s[%s].%s" % (coll, idx, props[prop])


def cv_operand_src(kind: int, rest: list[int]) -> str | None:
    """The value of Control Variables (RPG Maker's form: p5 onwards)."""
    n = len(rest)
    if kind in (0, 1, 2) and n == 2 and rest[1] == 0:
        return operand_src(kind, rest[0], 0)
    if kind == 7 and n == 2 and rest[1] == 0:
        if rest[0] in OTHER_SRC:
            return "%s.%s" % OTHER_SRC[rest[0]]
        if rest[0] in MANIAC_OTHER:
            return "%s.%s" % MANIAC_OTHER[rest[0]]
        return None
    if kind == 3 and n == 2:
        return "randint(%d, %d)" % tuple(rest)
    if kind == 3 and n == 3 and rest[2] != 0 and _fits(rest[2], 2):
        a, b = _mv(rest[2], 0, rest[0]), _mv(rest[2], 1, rest[1])
        return None if a is None or b is None else "randint(%s, %s)" % (a, b)
    if kind in CV_SELECTED and n == 2:
        if kind == 9:
            return None  # the party member operand comes from the Maniac Patch: written with its selector
        return _selected_src(kind, rest[0], rest[1], 0)
    if kind in CV_SELECTED and n == 3 and (rest[2] != 0 or kind == 9):
        return _selected_src(kind, rest[0], rest[1], rest[2])
    if kind == 10 and n == 2 and rest[1] in (0, 1):
        return sw_src(rest[0]) if rest[1] == 0 else "switches[%s]" % var_src(rest[0])
    return _maniac_operand_src(kind, rest)


def _maniac_operand_src(kind: int, r: list[int]) -> str | None:
    """Operands 11-25 (Maniac Patch): maths on values."""
    n = len(r)
    if kind in (11, 16, 17) and n == 3 and _fits(r[2], 2):
        a, b = _mv(r[2], 0, r[0]), _mv(r[2], 1, r[1])
        if a is None or b is None:
            return None
        return "%s ** %s" % (_paren(a), _paren(b)) if kind == 11 else "%s(%s, %s)" % (CV_FUNCTIONS[kind], a, b)
    if kind == 12 and n == 3 and _fits(r[2], 1):
        a = _mv(r[2], 0, r[0])
        return None if a is None else "sqrt(%s, %d)" % (a, r[1])
    if kind in (13, 14) and n == 4 and _fits(r[2], 2):
        a, b = _mv(r[2], 0, r[0]), _mv(r[2], 1, r[3])
        return None if a is None or b is None else "%s(%s, %s, %d)" % (CV_FUNCTIONS[kind], a, b, r[1])
    if kind == 15 and n == 4 and _fits(r[3], 2):
        a, b = _mv(r[3], 0, r[0]), _mv(r[3], 1, r[1])
        return None if a is None or b is None else "atan2(%s, %s, %d)" % (a, b, r[2])
    if kind == 18 and n == 2 and _fits(r[1], 1):
        a = _mv(r[1], 0, r[0])
        return None if a is None else "abs(%s)" % a
    if kind == 19 and n == 4 and 1 <= r[0] <= 10 and _fits(r[3], 2):
        a, b = _mv(r[3], 0, r[1]), _mv(r[3], 1, r[2])
        return None if a is None or b is None else "%s %s %s" % (a, CV_BINARY[r[0]], _paren(b))
    if kind == 20 and n == 6 and 0 <= r[0] < len(COMPARE) and _fits(r[5], 4):
        a, b, c, d = (_mv(r[5], k, r[k + 1]) for k in range(4))
        if None in (a, b, c, d):
            return None
        return "%s if %s %s %s else %s" % (c, a, COMPARE[r[0]], b, d)
    if kind == 22 and n == 5 and _fits(r[0], 4):
        args = [_mv(r[0], k, r[k + 1]) for k in range(4)]
        return None if None in args else "lerp(%s)" % ", ".join(args)  # type: ignore[arg-type]
    if kind in CV_ARRAYS and n == 3 and _fits(r[0], 2):
        a, b = _mv(r[0], 0, r[1]), _mv(r[0], 1, r[2])
        return None if a is None or b is None else "%s(%s, %s)" % (CV_ARRAYS[kind], a, b)
    return None


def _paren(src: str) -> str:
    return "(%s)" % src if src.startswith("-") else src


def cv_operand_val(v) -> tuple[int, list[int]]:
    """(operand, its parameters from p5) of a Control Variables value, RPG Maker's form."""
    if isinstance(v, bool):
        v = int(v)
    if is_ref(v, "switches"):
        if int_index(v, "switches") is not None:
            return 10, [v.index, 0]
        if var_index(v.index) is not None:
            return 10, [v.index.index, 1]
    if isinstance(v, Call) and v.target is None and v.name == "randint":
        a = K.Args(v, ["a", "b"])
        (ma, x), (mb, y) = mval_val(a.req("a"), "randint(): a"), mval_val(a.req("b"), "randint(): b")
        return 3, [x, y] + ([_packed(ma, mb)] if ma or mb else [])
    if isinstance(v, Attr):
        found = _selected_val(v)
        if found is not None:
            return found
        o = v.obj
        if isinstance(o, Sym) and (o.name, v.name) in {x: k for k, x in MANIAC_OTHER.items()}:
            return 7, [{x: k for k, x in MANIAC_OTHER.items()}[(o.name, v.name)], 0]
    if isinstance(v, BinOp):
        if v.op == "**":
            (ma, a), (mb, b) = mval_val(v.left, "a ** b: a"), mval_val(v.right, "a ** b: b")
            return 11, [a, b, _packed(ma, mb)]
        (ma, a), (mb, b) = mval_val(v.left, "a %s b: a" % v.op), mval_val(v.right, "a %s b: b" % v.op)
        return 19, [CV_BINARY.index(v.op), a, b, _packed(ma, mb)]
    if isinstance(v, IfExp) and isinstance(v.test, Cmp):
        vals = [mval_val(x, "a if b else c") for x in (v.test.left, v.test.right, v.body, v.orelse)]
        return 20, [COMPARE.index(v.test.op)] + [x for _, x in vals] + [_packed(*(m for m, _ in vals))]
    if isinstance(v, Call) and v.target is None and v.name in CV_FUNCTIONS.values():
        return _function_val(v)
    if isinstance(v, Call) and v.target is None and v.name in CV_ARRAYS.values():
        a = K.Args(v, ["first", "count"])
        (ma, x), (mb, y) = mval_val(a.req("first"), "%s(): first" % v.name), mval_val(a.req("count"), "count")
        return {n: k for k, n in CV_ARRAYS.items()}[v.name], [_packed(ma, mb), x, y]
    kind, p5, p6 = operand_val(v)
    return kind, [p5, p6]


def _selected_val(v: Attr) -> tuple[int, list[int]] | None:
    """items[3].count, actors[variables.hero].level, this.x, party[0].hp...: (operand, parameters)."""
    o, name = v.obj, v.name
    if name in MANIAC_EVENT_PROPS:
        cid = char_id(o)
        if cid is not None:
            return 6, [cid, MANIAC_EVENT_PROPS.index(name)]
        if is_ref(o, "events") and not is_int(o.index) and _MANIAC_SELECTORS.get():
            m, x = mval_val(o.index, "events[...]", 3)
            return 6, [x, MANIAC_EVENT_PROPS.index(name), m]
    for kind in (4, 5, 8, 9):
        coll, props = CV_SELECTED[kind]
        if name in props and is_ref(o, coll) and (is_int(o.index) or _MANIAC_SELECTORS.get()):
            m, x = mval_val(o.index, "%s[...]" % coll)
            return kind, [x, props.index(name)] + ([m] if m or kind == 9 else [])
    return None


def _function_val(v: Call) -> tuple[int, list[int]]:
    kind = {n: k for k, n in CV_FUNCTIONS.items()}[v.name]
    if kind in (16, 17):
        a = K.Args(v, ["a", "b"])
        (ma, x), (mb, y) = mval_val(a.req("a"), "%s(): a" % v.name), mval_val(a.req("b"), "%s(): b" % v.name)
        return kind, [x, y, _packed(ma, mb)]
    if kind == 12:
        a = K.Args(v, ["value", "scale"])
        m, x = mval_val(a.req("value"), "sqrt(): value")
        return kind, [x, a.int("scale"), m]
    if kind in (13, 14):
        a = K.Args(v, ["angle", "divisor", "scale"])
        (ma, x), (mb, y) = mval_val(a.req("angle"), "angle"), mval_val(a.req("divisor"), "divisor")
        return kind, [x, a.int("scale"), _packed(ma, mb), y]
    if kind == 15:
        a = K.Args(v, ["y", "x", "scale"])
        (ma, x), (mb, y) = mval_val(a.req("y"), "atan2(): y"), mval_val(a.req("x"), "atan2(): x")
        return kind, [x, y, a.int("scale"), _packed(ma, mb)]
    if kind == 18:
        a = K.Args(v, ["value"])
        m, x = mval_val(a.req("value"), "abs(): value")
        return kind, [x, m]
    a = K.Args(v, ["a", "b", "num", "den"])  # lerp
    vals = [mval_val(a.req(k), "lerp(): " + k) for k in ("a", "b", "num", "den")]
    return kind, [_packed(*(m for m, _ in vals))] + [x for _, x in vals]


def cv_src(p: list[int]) -> str | None:
    """Control Variables (RPG Maker's form) as a statement."""
    if len(p) < 7:
        return None
    t = target_src("variables", *p[:3])
    if t is None or not 0 <= p[3] < len(VAR_OPS):
        return None
    operand = cv_operand_src(p[4], list(p[5:]))
    if operand is None:
        return None
    return "%s %s %s" % (t, VAR_OPS[p[3]], operand)


# --------------------------------------------------------------------------
# Conditions (Conditional Branch)
# --------------------------------------------------------------------------

ACTOR_CHECKS = {2: "level", 3: "hp"}
ACTOR_METHODS = {4: "knows", 5: "has_equipped", 6: "has_state"}
VEHICLES = ["boat", "ship", "airship"]


def condition_src(p: list[int], string: str) -> str | None:
    """Conditional Branch params[0:5] (+ string) -> Python condition."""
    kind, a, b, c, d = p[:5]
    if kind == 15:
        return _string_condition_src(a, b, c, d, string)
    if string and not (kind == 5 and b == 1):
        return None
    if kind == 0 and b in (0, 1) and c == d == 0:
        return sw_src(a) if b == 0 else "not " + sw_src(a)
    if kind == 1 and b in (0, 1) and 0 <= d < len(COMPARE):
        rhs = str(c) if b == 0 else var_src(c)
        return "%s %s %s" % (var_src(a), COMPARE[d], rhs)
    if kind in (2, 3, 10) and b in (0, 1) and c == d == 0:
        lhs = {2: "timer1.seconds", 3: "party.gold", 10: "timer2.seconds"}[kind]
        return "%s %s %d" % (lhs, (">=", "<=")[b], a)
    if kind == 4 and b in (0, 1) and c == d == 0:
        return "items[%d] %s party" % (a, ("in", "not in")[b])
    if kind == 5 and d == 0:
        if b == 0 and c == 0:
            return "actors[%d] in party" % a
        if b == 1 and c == 0:
            return "actors[%d].name == %s" % (a, pystr(string))
        if b in ACTOR_CHECKS:
            return "actors[%d].%s >= %d" % (a, ACTOR_CHECKS[b], c)
        if b in ACTOR_METHODS:
            return "actors[%d].%s(%d)" % (a, ACTOR_METHODS[b], c)
    if kind == 6 and 0 <= b < 4 and c == d == 0:
        return "%s.facing == %s" % (char_src(a), pystr(K.DIRECTIONS[b]))
    if kind == 7 and 0 <= a < 3 and b == c == d == 0:
        return "player.vehicle == %s" % pystr(VEHICLES[a])
    if kind == 8 and a == b == c == d == 0:
        return "started_by_action_key"
    if kind == 9 and a == b == c == d == 0:
        return "bgm_looped"
    return "cond(%d, %d, %d, %d, %d)" % (kind, a, b, c, d)


# Maniac Patch: string variables compared (Conditional Branch 15): p1 how the left one
# (0 strings[p2], 1 strings[variables[p2]]) and the right one are given (bits 4-7: 0 the
# command's text, 1 strings[p3], 2 strings[variables[p3]]); p4 0 ==, 1 !=, 2 contains,
# 3 doesn't contain (+256: ignoring case, kept raw)


def _string_ref_src(mode: int, n: int) -> str | None:
    return str_src(n) if mode == 0 else "strings[%s]" % var_src(n) if mode == 1 else None


def _string_condition_src(modes: int, left: int, right: int, op: int, text: str) -> str | None:
    if not _fits(modes, 2) or not 0 <= op <= 3:
        return None
    lhs = _string_ref_src(modes & 0xF, left)
    rmode = modes >> 4
    if rmode == 0:
        rhs = pystr(text) if right == 0 else None
    else:
        rhs = None if text else _string_ref_src(rmode - 1, right)
    if lhs is None or rhs is None:
        return None
    if op < 2:
        return "%s %s %s" % (lhs, ("==", "!=")[op], rhs)
    return "%s %s %s" % (rhs, ("in", "not in")[op - 2], lhs)


def _string_ref_val(v) -> tuple[int, int] | None:
    if is_ref(v, "strings"):
        if is_int(v.index):
            return 0, v.index
        if var_index(v.index) is not None:
            return 1, v.index.index
    return None


def _string_condition_val(left, op: int, right, ctx: Ctx, node) -> tuple[list[int], bytes]:
    lhs = _string_ref_val(left)
    if lhs is None:
        raise _err("a string comparison tests strings[id] (or strings[variables[id]])", node)
    if isinstance(right, str):
        return [15, lhs[0], lhs[1], 0, op], ctx.enc(right, getattr(node, "node", None))
    rhs = _string_ref_val(right)
    if rhs is None:
        raise _err("strings[id] is compared with a text, strings[id] or strings[variables[id]]", node)
    return [15, lhs[0] | (rhs[0] + 1) << 4, lhs[1], rhs[1], op], b""


def condition_val(v, ctx: Ctx) -> tuple[list[int], bytes]:
    """Python condition -> (Conditional Branch params[0:5], string)."""
    if isinstance(v, Cmp) and v.op in ("==", "!=") and is_ref(v.left, "strings"):
        return _string_condition_val(v.left, ("==", "!=").index(v.op), v.right, ctx, v)
    if isinstance(v, In) and is_ref(v.right, "strings"):
        return _string_condition_val(v.right, 3 if v.negated else 2, v.left, ctx, v)
    if is_ref(v, "switches") and var_index(v) is None and int_index(v, "switches") is not None:
        return [0, v.index, 0, 0, 0], b""
    if isinstance(v, Not) and int_index(v.value, "switches") is not None:
        return [0, v.value.index, 1, 0, 0], b""
    if isinstance(v, Cmp):
        left, op, right = v.left, v.op, v.right
        if var_index(left) is not None:
            if is_int(right):
                return [1, left.index, 0, right, COMPARE.index(op)], b""
            if var_index(right) is not None:
                return [1, left.index, 1, right.index, COMPARE.index(op)], b""
            if _pointer_ref(right) is None:
                raise _err("compare a variable with a number or another variables[id]", v)
        if isinstance(left, Attr):
            o, name = left.obj, left.name
            sym = o.name if isinstance(o, Sym) else None
            if (sym, name) in (("timer1", "seconds"), ("party", "gold"), ("timer2", "seconds")):
                if op not in (">=", "<=") or not is_int(right):
                    raise _err("%s can only be compared with >= or <= and a number" % show(left), v)
                kind = {"timer1": 2, "party": 3, "timer2": 10}[sym]
                return [kind, right, (">=", "<=").index(op), 0, 0], b""
            if int_index(o, "actors") is not None:
                if name == "name" and op == "==" and isinstance(right, str):
                    return [5, o.index, 1, 0, 0], ctx.enc(right, v.node)
                if name in ACTOR_CHECKS.values() and op == ">=" and is_int(right):
                    sub = {n: k for k, n in ACTOR_CHECKS.items()}[name]
                    return [5, o.index, sub, right, 0], b""
                raise _err('actor conditions: actors[id].name == "...", actors[id].level >= n, actors[id].hp >= n', v)
            if name == "facing" and char_id(o) is not None and op == "==" and right in K.DIRECTIONS:
                return [6, char_id(o), K.DIRECTIONS.index(right), 0, 0], b""
            if sym == "player" and name == "vehicle" and op == "==" and right in VEHICLES:
                return [7, VEHICLES.index(right), 0, 0, 0], b""
    if isinstance(v, In) and isinstance(v.right, Sym) and v.right.name == "party":
        if int_index(v.left, "items") is not None:
            return [4, v.left.index, 1 if v.negated else 0, 0, 0], b""
        if int_index(v.left, "actors") is not None and not v.negated:
            return [5, v.left.index, 0, 0, 0], b""
    if isinstance(v, Call) and int_index(v.target, "actors") is not None and v.name in ACTOR_METHODS.values():
        sub = {n: k for k, n in ACTOR_METHODS.items()}[v.name]
        a = K.Args(v, ["id"])
        return [5, v.target.index, sub, a.int("id"), 0], b""
    if isinstance(v, Sym) and v.name in ("started_by_action_key", "bgm_looped"):
        return [8 if v.name == "started_by_action_key" else 9, 0, 0, 0, 0], b""
    if isinstance(v, Call) and v.name == "cond" and v.target is None:
        if len(v.args) != 5 or v.kwargs or not all(is_int(x) for x in v.args):
            raise _err("cond() takes exactly 5 integer parameters", v)
        return list(v.args), b""
    if isinstance(v, And):
        raise _err("a branch can only test one condition; nest ifs instead of using 'and'", v)
    tested = [v.left, v.right] if isinstance(v, Cmp) else [v.value if isinstance(v, Not) else v]
    pointer = next((p for p in map(_pointer_ref, tested) if p is not None), None)
    if pointer is not None and pointer.coll == "variables":
        raise _err(
            "a branch can't test %s: RPG Maker branches only test a fixed variable. "
            "Copy it first: `variables.tmp = %s` then test variables.tmp" % (show(pointer), show(pointer)),
            v,
        )
    if pointer is not None:
        raise _err(
            "RPG Maker can't read %s: a switch can be set through a pointer, never read. "
            "Keep flags you index in variables (0 / 1) instead" % show(pointer),
            v,
        )
    raise _err(
        "unsupported condition %s. Examples: switches[1], not switches[1], variables[1] >= 10, "
        "variables[1] == variables[2], party.gold >= 100, items[3] in party, actors[1] in party, "
        'this.facing == "up", started_by_action_key' % show(v),
        v,
    )


# --------------------------------------------------------------------------
# Page conditions:  @page(when=switches[1] and variables[2] >= 5 and ...)
# --------------------------------------------------------------------------


def page_condition_src(c: dict[str, Any]) -> str | None:
    if "switch_b" in c and "switch_a" not in c:
        return None
    parts = []
    for k in ("switch_a", "switch_b"):
        if k in c:
            parts.append(sw_src(c[k]))
    if "variable" in c:
        vid, op, val = c["variable"]
        if not 0 <= op < len(COMPARE):
            return None
        parts.append("%s %s %d" % (var_src(vid), COMPARE[op], val))
    if "item" in c:
        parts.append("items[%d] in party" % c["item"])
    if "actor" in c:
        parts.append("actors[%d] in party" % c["actor"])
    if "timer" in c:
        parts.append("timer1.seconds <= %d" % c["timer"])
    if "timer2" in c:
        parts.append("timer2.seconds <= %d" % c["timer2"])
    return " and ".join(parts) if parts else None


def page_condition_val(v, ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    parts = v.values if isinstance(v, And) else [v]
    for p in parts:

        def put(key, value, p=p):
            if key in out:
                raise _err("a page can only have one %s condition" % key.replace("_", " "), p)
            out[key] = value

        if int_index(p, "switches") is not None:
            put("switch_a" if "switch_a" not in out else "switch_b", p.index)
        elif isinstance(p, Cmp) and var_index(p.left) is not None and is_int(p.right):
            if ctx.engine == "2k" and p.op != ">=":
                raise _err("RPG Maker 2000 page conditions only support variables[id] >= value", p)
            put("variable", (p.left.index, COMPARE.index(p.op), p.right))
        elif (
            isinstance(p, In)
            and isinstance(p.right, Sym)
            and p.right.name == "party"
            and not p.negated
            and int_index(p.left, "items") is not None
        ):
            put("item", p.left.index)
        elif (
            isinstance(p, In)
            and isinstance(p.right, Sym)
            and p.right.name == "party"
            and not p.negated
            and int_index(p.left, "actors") is not None
        ):
            put("actor", p.left.index)
        elif (
            isinstance(p, Cmp)
            and isinstance(p.left, Attr)
            and isinstance(p.left.obj, Sym)
            and p.left.obj.name in ("timer1", "timer2")
            and p.left.name == "seconds"
            and p.op == "<="
            and is_int(p.right)
        ):
            put("timer" if p.left.obj.name == "timer1" else "timer2", p.right)
        else:
            raise _err(
                "unsupported page condition %s. A page can require: up to two switches[id], "
                "variables[id] >= value, items[id] in party, actors[id] in party, "
                "timer1.seconds <= n, timer2.seconds <= n (joined with 'and')" % show(p),
                p,
            )
    return out


# --------------------------------------------------------------------------
# Statements that map to one command
# --------------------------------------------------------------------------


def _move_tail(moves) -> list[int]:
    from .lcf import write_move_commands

    return list(write_move_commands(moves))


def decode_candidates(c: Command, ctx: Ctx, in_loop: bool = False) -> Iterator[str]:
    """The Pythonic statements one command may be written as, best first (each still to
    be checked by compiling it back)."""
    from . import maniac_ext, named

    yield from named.decode_all(c, ctx)
    src = decode_stmt(c, ctx, in_loop, named_forms=False)
    if src is not None:
        yield src
    yield from maniac_ext.decode(c, ctx, in_loop)


def decode_stmt(c: Command, ctx: Ctx, in_loop: bool = False, named_forms: bool = True) -> str | None:
    """Pythonic statement for one command, or None."""
    from . import named

    if named_forms:
        src = named.decode(c, ctx)
        if src is not None:
            return src
    p, code = c.params, c.code
    s = ctx.dec(c.string) if c.string else ""
    if c.string and code not in (10610, 10620):
        return None
    if code == 10220:
        return cv_src(p)
    if code == 10210 and len(p) == 4:
        t = target_src("switches", *p[:3])
        if t is None or p[3] not in (0, 1, 2):
            return None
        return "%s.toggle()" % t if p[3] == 2 else "%s = %s" % (t, ("True", "False")[p[3]])
    if code == 10150 and len(p) == 2:
        return "%s = input_number(digits=%d)" % (var_src(p[1]), p[0])
    if code == 10310 and len(p) == 3 and p[0] in (0, 1):
        v = value_src(p[1], p[2])
        return None if v is None else "party.gold %s %s" % ("+=" if p[0] == 0 else "-=", v)
    if code == 10320 and len(p) == 5 and p[0] in (0, 1):
        item, amount = value_src(p[1], p[2]), value_src(p[3], p[4])
        if item is None or amount is None:
            return None
        return "items[%s].count %s %s" % (item, "+=" if p[0] == 0 else "-=", amount)
    if code == 10330 and len(p) == 3 and p[0] in (0, 1):
        who = value_src(p[1], p[2])
        return None if who is None else "party.%s(actors[%s])" % (("add", "remove")[p[0]], who)
    if code in (10410, 10420) and len(p) == 6 and p[2] in (0, 1) and p[5] in (0, 1):
        who = _actor_target_src(p[0], p[1])
        amount = value_src(p[3], p[4])
        if (
            who is None
            or amount is None
            or (p[2] == 1 and p[3] == 0 and p[4] <= 0)
            or (p[2] == 0 and p[3] == 0 and p[4] < 0)
        ):
            return None
        delta = ("-" if p[2] == 1 else "") + amount
        kw = ", message=True" if p[5] else ""
        return "%s(%s, %s%s)" % ("change_exp" if code == 10410 else "change_level", who, delta, kw)
    if code == 10490 and len(p) == 2:
        who = _actor_target_src(p[0], p[1])
        return None if who is None else "full_heal(%s)" % who
    if code in (10610, 10620) and len(p) == 1:
        return "actors[%d].%s = %s" % (p[0], "name" if code == 10610 else "title", pystr(s))
    if code == 10860 and len(p) in (4, 5) and (len(p) == 4) == (ctx.engine == "2k"):
        ev, mode, x, y = p[:4]
        if mode not in (0, 1):
            return None
        pos = ("%d, %d" % (x, y)) if mode == 0 else "%s, %s" % (var_src(x), var_src(y))
        kw = ""
        if len(p) == 5 and p[4]:
            if not 0 <= p[4] < len(K.TELEPORT_DIRECTIONS):
                return None
            kw = ", direction=%s" % pystr(K.TELEPORT_DIRECTIONS[p[4]])
        return "%s.set_location(%s%s)" % (char_src(ev), pos, kw)
    if code == 11330 and len(p) >= 4:
        return _move_src(c, ctx)
    if code == 11210 and len(p) == 4 and p[2] in (0, 1) and p[3] in (0, 1):
        kw = (", wait=True" if p[2] else "") + (", whole_map=True" if p[3] else "")
        return "%s.show_animation(%d%s)" % (char_src(p[1]), p[0], kw)
    if code == 12330 and len(p) == 3:
        kind, a, b = p
        if kind == 0 and b == 0:
            return "%s()" % ce_src(a)
        if kind == 1:
            cur = _SELF_EVENT.get()
            name = "self" if cur is not None and cur[1] == a else event_name(a)
            if name and 1 <= b <= _EVENT_PAGE_COUNTS.get().get(a, 0):
                return "%s.page_%d()" % (name, b)  # a page of this event, or of a named event of this map
            return "%s.call(%d)" % (char_src(a), b)
        if kind == 2:
            return "events[%s].call(%s)" % (var_src(a), var_src(b))
        return None
    if code in (10910, 10920) and len(p) == 4 and p[0] in (0, 1):
        pos = ("%d, %d" % (p[1], p[2])) if p[0] == 0 else "%s, %s" % (var_src(p[1]), var_src(p[2]))
        return "%s = %s(%s)" % (var_src(p[3]), "terrain_id" if code == 10910 else "event_id_at", pos)
    if code == 11610:
        return _key_input_src(p, ctx)
    if code == 11310 and len(p) == 1 and p[0] in (0, 1):
        return "player.visible = %s" % ("True" if p[0] else "False")
    if code in GAME_FLAGS and len(p) == 1 and p[0] in (0, 1):
        return "game.%s = %s" % (GAME_FLAGS[code], "True" if p[0] else "False")
    if code == 10230:
        return _timer_src(p, ctx)
    if code == 12320 and not p:
        return "this.erase()"
    if code == 12310 and not p:
        return "return"
    if code == 12220 and not p:
        return "break" if in_loop else "break_loop()"
    return None


def _actor_target_src(mode: int, v: int) -> str | None:
    if mode == 0:
        return "party" if v == 0 else None
    if mode == 1:
        return "actors[%d]" % v
    if mode == 2:
        return "actors[%s]" % var_src(v)
    return None


def _actor_target_val(v) -> tuple[int, int]:
    if isinstance(v, Sym) and v.name == "party":
        return 0, 0
    n = int_index(v, "actors")
    if n is not None:
        return 1, n
    if is_ref(v, "actors") and var_index(v.index) is not None:
        return 2, v.index.index
    raise _err("expected party, actors[id] or actors[variables[id]] (got %s)" % show(v), v)


def _move_src(c: Command, ctx: Ctx) -> str | None:
    from .lcf import LcfError, read_move_commands, write_move_commands

    target, freq, repeat, skip = c.params[:4]
    tail = c.params[4:]
    if any(not 0 <= b <= 255 for b in tail) or repeat not in (0, 1) or skip not in (0, 1):
        return None
    try:
        moves = read_move_commands(bytes(tail))
    except (LcfError, IndexError):
        return None
    if list(write_move_commands(moves)) != tail:
        return None
    args = [m.src for m in K.moves_to_src(moves, ctx)]
    kw = ["frequency=%d" % freq]
    if repeat:
        kw.append("repeat=True")
    if skip:
        kw.append("skippable=True")
    return "%s.move(%s)" % (char_src(target), ", ".join(args + kw))


def encode_stmt(stmt: ast.stmt, ctx: Ctx) -> Command | None:
    """Compile a pythonic single-command statement, or return None if the
    statement is a plain function call handled by the command catalog."""

    def C(code, params, string=b""):
        return Command(code=code, params=list(params), string=string)

    if isinstance(stmt, ast.Return):
        if stmt.value is not None:
            raise CompileError("return takes no value (it ends the event)", stmt)
        return C(12310, [])
    if isinstance(stmt, ast.Break):
        return C(12220, [])
    if isinstance(stmt, ast.Continue):
        return C(12220, [1, 0])  # Maniac Patch: Break Loop, skip to the next turn

    if isinstance(stmt, ast.Assign):
        if len(stmt.targets) != 1:
            raise CompileError("assign one target at a time", stmt)
        target, value = evaluate(stmt.targets[0]), evaluate(stmt.value)
        if isinstance(target, Attr) and isinstance(target.obj, Sym):
            obj, attr = target.obj.name, target.name
            if (obj, attr) == ("player", "visible") or (obj == "game" and attr in GAME_FLAGS.values()):
                if not isinstance(value, bool):
                    raise CompileError("%s.%s is True or False" % (obj, attr), stmt)
                code = 11310 if obj == "player" else {v: k for k, v in GAME_FLAGS.items()}[attr]
                return C(code, [int(value)])
        if (
            is_ref(target, "variables")
            and isinstance(value, Call)
            and value.target is None
            and value.name in ("terrain_id", "event_id_at", "key_input")
        ):
            mode, a, _ = target_val(target)
            if mode != 0:
                raise CompileError("%s() stores into a single variables[id]" % value.name, stmt)
            if value.name == "key_input":
                return C(11610, _key_input_params(value, a, ctx))
            ar = K.Args(value, ["x", "y"])
            x, y = ar.req("x"), ar.req("y")
            if var_index(x) is not None and var_index(y) is not None:
                pos = [1, x.index, y.index]
            else:
                pos = [0, ar.int("x"), ar.int("y")]
            return C(10910 if value.name == "terrain_id" else 10920, pos + [a])
        if is_ref(target, "variables"):
            mode, a, b = target_val(target)
            if isinstance(value, Call) and value.name == "input_number" and value.target is None:
                if mode != 0:
                    raise CompileError("input_number() stores into a single variables[id]", stmt)
                ar = K.Args(value, ["digits"])
                return C(10150, [ar.int("digits"), a])
            if mode == 0:
                from . import maniac

                cmd = maniac.encode_string_query(a, value, ctx)  # len(strings[1]), int(...), .find()...
                if cmd is not None:
                    return cmd
            kind, rest = cv_operand_val(value)
            return C(10220, [mode, a, b, 0, kind] + rest)
        if is_ref(target, "strings"):
            from . import maniac

            return maniac.encode_string_value(target, 0, value, ctx, stmt)
        if is_ref(target, "switches"):
            mode, a, b = target_val(target)
            if isinstance(value, bool):
                return C(10210, [mode, a, b, 0 if value else 1])
            if isinstance(value, Not) and value.value == target.model_copy(update={"node": value.value.node}):
                return C(10210, [mode, a, b, 2])
            raise CompileError("switches can be set to True or False (or toggled with .toggle())", stmt)
        if (
            isinstance(target, Attr)
            and int_index(target.obj, "actors") is not None
            and target.name in ("name", "title")
        ):
            if not isinstance(value, str):
                raise CompileError("actor %s must be a string" % target.name, stmt)
            return C(10610 if target.name == "name" else 10620, [target.obj.index], ctx.enc(value, stmt))
        raise CompileError(
            "cannot assign to %s. Assignable: variables[...], switches[...], "
            "actors[id].name, actors[id].title" % ast.unparse(stmt.targets[0]),
            stmt,
        )

    if isinstance(stmt, ast.AugAssign):
        target, value = evaluate(stmt.target), evaluate(stmt.value)
        if type(stmt.op) not in AUG_OPS:
            hint = " (use //= for division)" if isinstance(stmt.op, ast.Div) else ""
            raise CompileError("unsupported operator%s" % hint, stmt)
        op = AUG_OPS[type(stmt.op)]
        if is_ref(target, "variables"):
            mode, a, b = target_val(target)
            kind, rest = cv_operand_val(value)
            return C(10220, [mode, a, b, VAR_OPS.index(op), kind] + rest)
        if is_ref(target, "strings") and op == "+=":
            from . import maniac

            return maniac.encode_string_value(target, 1, value, ctx, stmt)
        if op not in ("+=", "-="):
            raise CompileError("only += and -= are supported here", stmt)
        sign = 0 if op == "+=" else 1
        if (
            isinstance(target, Attr)
            and isinstance(target.obj, Sym)
            and target.obj.name == "party"
            and target.name == "gold"
        ):
            k, v = value_val(value, "gold")
            return C(10310, [sign, k, v])
        if isinstance(target, Attr) and is_ref(target.obj, "items") and target.name == "count":
            ik, iv = value_val(target.obj.index, "item id")
            ak, av = value_val(value, "amount")
            return C(10320, [sign, ik, iv, ak, av])
        raise CompileError(
            "cannot change %s. Supported: variables[...] op= value, party.gold += n, "
            "items[id].count += n" % ast.unparse(stmt.target),
            stmt,
        )

    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = evaluate(stmt.value)
        from . import named

        cmd = named.encode(call, ctx)
        if cmd is not None:
            return cmd
        from . import maniac_ext

        cmd = maniac_ext.encode_call(call, ctx)
        if cmd is not None:
            return cmd
        t = call.target
        event = _event_of(t)
        if event is not None:
            n = page_number(event, call.name, call.node)
            if n is not None:  # Gary.page_3(), self.page_3(): Call Event
                K.Args(call, [])
                return C(12330, [1, need_char(t), n])
        if isinstance(t, Sym) and t.name == "this" and _PAGE_NAME.fullmatch(call.name):
            n = _PAGE_NAME.fullmatch(call.name).group(1)  # type: ignore[union-attr]
            raise _err("this is whichever event runs the code: this.call(%s) runs its page %s" % (n, n), call)
        if isinstance(t, Sym) and t.name == "dyn":
            return C(12410, [], ctx.enc(dynrpg_text(call), stmt))
        if is_ref(t, "strings"):
            from . import maniac

            return maniac.encode_string_call(call, ctx)
        if t is None:
            if call.name in ("change_exp", "change_level"):
                a = K.Args(call, ["actor", "amount", "message"])
                mode, who = _actor_target_val(a.req("actor"))
                amount = a.req("amount")
                op = 0
                if isinstance(amount, Neg):
                    op, amount = 1, amount.value
                elif isinstance(amount, Pos):
                    amount = amount.value
                elif is_int(amount) and amount < 0:
                    op, amount = 1, -amount
                k, v = value_val(amount, "amount")
                return C(10410 if call.name == "change_exp" else 10420, [mode, who, op, k, v, int(a.bool("message"))])
            if call.name == "full_heal":
                a = K.Args(call, ["actor"])
                return C(10490, list(_actor_target_val(a.req("actor"))))
            if call.name == "break_loop":
                K.Args(call, [])
                return C(12220, [])
            return None
        if isinstance(t, Sym) and t.name in ("timer1", "timer2") and call.name in TIMER_OPS:
            return C(10230, _timer_params(call, t.name, ctx))
        if call.name == "toggle" and is_ref(t, "switches"):
            K.Args(call, [])
            mode, a, b = target_val(t)
            return C(10210, [mode, a, b, 2])
        if call.name in ("add", "remove") and isinstance(t, Sym) and t.name == "party":
            a = K.Args(call, ["actor"])
            who = a.req("actor")
            if not is_ref(who, "actors"):
                raise _err("party.%s() takes actors[id]" % call.name, call)
            k, v = value_val(who.index, "actor id")
            return C(10330, [0 if call.name == "add" else 1, k, v])
        if call.name == "__call__" and int_index(t, "common_events") is not None:
            K.Args(call, [])
            return C(12330, [0, t.index, 0])
        if call.name == "call":
            a = K.Args(call, ["page"])
            page = a.req("page")
            if is_ref(t, "events") and var_index(t.index) is not None:
                if var_index(page) is None:
                    raise _err("events[variables[id]].call() also takes the page as variables[id]", call)
                return C(12330, [2, t.index.index, page.index])
            return C(12330, [1, need_char(t), a.int("page")])
        if call.name == "erase":
            K.Args(call, [])
            if not (isinstance(t, Sym) and t.name == "this"):
                raise _err("only this.erase() exists (Erase Event erases the running event)", call)
            return C(12320, [])
        if call.name == "move":
            a = K.Args(
                Call(name="move", args=[], kwargs=call.kwargs, node=call.node), ["frequency", "repeat", "skippable"]
            )
            moves = K.moves_from_values(call.args, ctx, call.node)
            return C(
                11330,
                [need_char(t), a.int("frequency", 6), int(a.bool("repeat")), int(a.bool("skippable"))]
                + _move_tail(moves),
            )
        if call.name == "set_location":
            a = K.Args(call, ["x", "y", "direction"])
            x, y = a.req("x"), a.req("y")
            if var_index(x) is not None and var_index(y) is not None:
                mode, xv, yv = 1, x.index, y.index
            else:
                mode, xv, yv = 0, a.int("x"), a.int("y")
            params = [need_char(t), mode, xv, yv]
            if ctx.engine != "2k" or "direction" in a.values:
                params.append(a.enum("direction", K.TELEPORT_DIRECTIONS, "retain"))
            return C(10860, params)
        if call.name == "show_animation":
            a = K.Args(call, ["animation", "wait", "whole_map"])
            return C(11210, [a.int("animation"), need_char(t), int(a.bool("wait")), int(a.bool("whole_map"))])
        raise _err("unknown method %s()" % ast.unparse(stmt.value.func), call)
    return None


# --------------------------------------------------------------------------
# DynRPG plugin calls:  @change_text "id", "\\v[1]", 0  <->  dyn.change_text("id", r"\v[1]", 0)
# --------------------------------------------------------------------------

_DYN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DYN_TOKEN = re.compile(r"^([NnVv]+)(\d+)$")
_DYN_STRING = re.compile(r'^"((?:[^"]|"")*)"$')  # a " inside a string is written ""
_DYN_NOT_IN_WORD = ('"', ",", "\n", "\r")


def _dyn_token_src(prefix: str, n: int) -> str:
    """``V12`` -> ``variables.name``, ``N3`` -> ``actors[3].name``; other spellings stay ``v[12]``."""
    if prefix == "V":
        return var_src(n)
    if prefix == "VV":
        return "variables[%s]" % var_src(n)
    if prefix == "N":
        return "actors[%d].name" % n
    if prefix == "NV":
        return "actors[%s].name" % var_src(n)
    return "%s[%d]" % (prefix, n)


def _dyn_token(v) -> str | None:
    """DynRPG token of a script value: ``variables.x`` -> ``V12``, ``actors[3].name`` -> ``N3``."""
    if isinstance(v, DynToken):
        return "%s%d" % (v.prefix, v.number)
    if var_index(v) is not None:
        return "V%d" % v.index
    if is_ref(v, "variables") and var_index(v.index) is not None:
        return "VV%d" % v.index.index
    if isinstance(v, Attr) and v.name == "name" and is_ref(v.obj, "actors"):
        if int_index(v.obj, "actors") is not None:
            return "N%d" % v.obj.index
        if var_index(v.obj.index) is not None:
            return "NV%d" % v.obj.index.index
    return None


def _split_dyn_args(text: str) -> list[str] | None:
    """The arguments of a DynRPG call as written: split at the commas outside strings, their
    spaces kept.  None when a string isn't closed."""
    args, cur, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    if quoted:
        return None
    args.append(cur)
    return args


def _dyn_arg_src(a: str) -> str | None:
    """Python form of one argument as written in the comment, None when none compiles back to it."""
    if m := _DYN_STRING.match(a):
        return pystr(m.group(1).replace('""', '"'))
    if _DYN_NUMBER.match(a):
        return a if (repr(float(a)) if "." in a else str(int(a))) == a else None
    if (m := _DYN_TOKEN.match(a)) and DYN_PREFIX.match(m.group(1)):
        return _dyn_token_src(m.group(1), int(m.group(2)))
    if not a.strip(" ") or any(ch in a for ch in _DYN_NOT_IN_WORD):
        return None
    if dynrpg.kind(a.replace(" ", "")) != dynrpg.WORD:
        return None  # a number or token with spaces (DynRPG drops them): not a word for ty
    return word_src(a)


def word_src(text: str) -> str:
    """``word.end``, or ``word(" Page 2")`` for a text that is no Python name."""
    name = text.isidentifier() and not keyword.iskeyword(text) and not text.startswith("__")
    if name and unicodedata.normalize("NFKC", text) == text:  # Python reads names in NFKC
        return "word." + text
    return "word(%s)" % pystr(text)


def dynrpg_src(line: str) -> str | None:
    """Python form of a one-line DynRPG comment, or None if it cannot be
    written back identically.  The first argument follows the name after one
    space, the others follow ", "; an argument that is no string, number or
    token is a word, its spaces kept (``word(" Page 2")``).  A plugin's own word
    is the "string" it reads: DynText's ``"fixed"`` is ``word.fixed``."""
    m = re.match(r"^@([A-Za-z_][A-Za-z0-9_]*)(?: (.*))?$", line)
    if not m:
        return None
    name, rest = m.group(1), m.group(2)
    args_src = []
    if rest is not None:
        args = _split_dyn_args(rest)
        if args is None or not all(a.startswith(" ") for a in args[1:]):
            return None
        for i, a in enumerate([args[0]] + [a[1:] for a in args[1:]]):
            words = p.words if (p := dynrpg.param(name, i)) else ()
            if (m := _DYN_STRING.match(a)) and m.group(1) in words:
                args_src.append(word_src(m.group(1)))  # DynText's "fixed": word.fixed
                continue
            if a in words:
                return None  # the bare word, that word.fixed doesn't write: the comment stays as is
            src = _dyn_arg_src(a)
            if src is None:
                return None
            args_src.append(src)
    return "dyn.%s(%s)" % (name, ", ".join(args_src))


def _dyn_word(v) -> str | None:
    """The text of ``word.end`` / ``word("Simu-Com 00")``, else None."""
    if isinstance(v, Attr) and isinstance(v.obj, Sym) and v.obj.name == "word":
        return v.name
    if not (isinstance(v, Call) and v.target is None and v.name == "word"):
        return None
    if v.kwargs or len(v.args) != 1 or not isinstance(v.args[0], str):
        raise _err('word() takes one text: word("Simu-Com 00")', v)
    if any(ch in v.args[0] for ch in _DYN_NOT_IN_WORD):
        raise _err('a word can\'t hold ", commas or line breaks: write a "string" instead', v)
    return v.args[0]


def dynrpg_text(call: Call) -> str:
    if call.kwargs:
        raise _err("DynRPG calls take positional arguments only", call)
    parts = []
    kinds = "numbers, strings, variables[...], actors[...].name or words (word.end)"
    for i, a in enumerate(call.args):
        if isinstance(a, str):
            parts.append('"%s"' % a.replace('"', '""'))
        elif isinstance(a, bool):
            raise _err("DynRPG arguments are %s" % kinds, call)
        elif isinstance(a, (int, float)):
            parts.append(repr(a) if isinstance(a, float) else str(a))
        elif _dyn_token(a) is not None:
            parts.append(_dyn_token(a))
        elif (text := _dyn_word(a)) is not None:
            p = dynrpg.param(call.name, i)
            # a plugin's own word, as it reads it (word.fixed: DynText's "fixed"), else as is, unquoted
            parts.append('"%s"' % text if p and text in p.words else text)
        else:
            raise _err("DynRPG arguments are %s (got %s)" % (kinds, show(a)), call)
    return "@" + call.name + (" " + ", ".join(parts) if parts else "")


# --------------------------------------------------------------------------
# Key input, timers, game flags
# --------------------------------------------------------------------------

GAME_FLAGS = {11820: "allow_teleport", 11840: "allow_escape", 11930: "allow_save", 11960: "allow_menu"}

# parameter index -> keyword, per editor generation (the length tells them apart)
KEYS_2K3 = {
    1: "wait",
    2: "directions",
    3: "decision",
    4: "cancel",
    5: "numbers",
    6: "operators",
    7: "time_variable",
    8: "count_time",
    9: "shift",
    10: "down",
    11: "left",
    12: "right",
    13: "up",
}
KEYS_2K = {
    1: "wait",
    2: "directions",
    3: "decision",
    4: "cancel",
    5: "shift",
    6: "down",
    7: "left",
    8: "right",
    9: "up",
}


def _key_flag(v: int) -> str:
    return {0: "False", 1: "True"}.get(v, str(v))  # Maniac Patch stores bit masks (2 = mouse)


def _key_input_src(p: list[int], ctx: Ctx) -> str | None:
    layouts = {14: KEYS_2K3, 10: KEYS_2K}
    if len(p) not in layouts:
        return None
    names = layouts[len(p)]
    kw = []
    for i, name in names.items():
        if name == "time_variable":
            if p[i]:
                kw.append("time_variable=%s" % var_src(p[i]))
        elif p[i]:
            kw.append("%s=%s" % (name, _key_flag(p[i])))
    if (len(p) == 10) != (ctx.engine == "2k"):
        kw.append("layout=%d" % (2000 if len(p) == 10 else 2003))
    return "%s = key_input(%s)" % (var_src(p[0]), ", ".join(kw))


def _key_input_params(call: Call, var: int, ctx: Ctx) -> list[int]:
    a = K.Args(call, sorted(set(KEYS_2K3.values()) | {"layout"}))
    layout = a.get("layout", 2000 if ctx.engine == "2k" else 2003)
    if layout not in (2000, 2003):
        raise CompileError("key_input(): layout is 2000 or 2003", call.node)
    names = KEYS_2K3 if layout == 2003 else KEYS_2K
    for k in a.values:
        if k != "layout" and k not in names.values():
            raise CompileError("key_input(): %s does not exist in the RPG Maker %d layout" % (k, layout), call.node)
    params = [var] + [0] * len(names)
    for i, name in names.items():
        v = a.get(name)
        if v is None:
            continue
        if name == "time_variable":
            n = var_index(v)
            if n is None:
                raise CompileError("key_input(): time_variable must be variables[id]", call.node)
            params[i] = n
        else:
            params[i] = int(v) if isinstance(v, (bool, int)) else a.int(name)
    return params


TIMER_OPS = ["set", "start", "stop"]


def _timer_src(p: list[int], ctx: Ctx) -> str | None:
    if len(p) == 6 and p[5] in (0, 1):
        timer = "timer%d" % (p[5] + 1)
    elif len(p) == 5 and ctx.engine == "2k":
        timer = "timer1"
    else:
        return None
    op, kind, value, visible, battle = p[:5]
    if op not in (0, 1, 2) or visible not in (0, 1) or battle not in (0, 1):
        return None
    seconds = value_src(kind, value)
    if seconds is None:
        return None
    args, kw = [], []
    if op == 0:
        args.append(seconds)
    elif kind or value:
        kw.append("seconds=%s" % seconds)
    if visible:
        kw.append("visible=True")
    if battle:
        kw.append("in_battle=True")
    return "%s.%s(%s)" % (timer, TIMER_OPS[op], ", ".join(args + kw))


def _timer_params(call: Call, timer: str, ctx: Ctx) -> list[int]:
    a = K.Args(call, ["seconds", "visible", "in_battle"])
    if call.name == "set" and "seconds" not in a.values:
        raise CompileError("timer.set() needs the number of seconds", call.node)
    kind, value = value_val(a.get("seconds", 0), "seconds")
    params = [TIMER_OPS.index(call.name), kind, value, int(a.bool("visible")), int(a.bool("in_battle"))]
    if ctx.engine != "2k" or timer == "timer2":
        params.append(0 if timer == "timer1" else 1)
    return params


# --------------------------------------------------------------------------
# Battle conditional branch:  if battle_condition(actors[1].can_act): ...
# --------------------------------------------------------------------------


def battle_condition_src(p: list[int]) -> str | None:
    kind, a, b, c, d = p[:5]
    if kind in (0, 1):
        inner = condition_src([kind, a, b, c, d], "")
        if inner is None or inner.startswith("cond("):
            return None
    elif kind == 2 and b == c == d == 0:
        inner = "actors[%d].can_act" % a
    elif kind == 3 and b == c == d == 0:
        inner = "enemies[%d].can_act" % a
    elif kind == 4 and b == c == d == 0:
        inner = "enemies[%d].targeted" % a
    elif kind == 5 and c == d == 0:
        inner = "actors[%d].uses_command(%d)" % (a, b)
    else:
        inner = "cond(%d, %d, %d, %d, %d)" % (kind, a, b, c, d)
    return "battle_condition(%s)" % inner


def battle_condition_val(v, ctx: Ctx) -> list[int]:
    if not (isinstance(v, Call) and v.name == "battle_condition" and v.target is None and len(v.args) == 1):
        raise _err("expected battle_condition(<condition>)", v)
    c = v.args[0]
    if isinstance(c, Attr) and c.name in ("can_act", "targeted"):
        if c.name == "can_act" and int_index(c.obj, "actors") is not None:
            return [2, c.obj.index, 0, 0, 0]
        if int_index(c.obj, "enemies") is not None:
            return [3 if c.name == "can_act" else 4, c.obj.index, 0, 0, 0]
    if isinstance(c, Call) and c.name == "uses_command" and int_index(c.target, "actors") is not None:
        return [5, c.target.index, K.Args(c, ["command"]).int("command"), 0, 0]
    params, string = condition_val(c, ctx)
    if params[0] not in (0, 1) and not (isinstance(c, Call) and c.name == "cond"):
        raise _err(
            "battle_condition() tests switches, variables, actors[id].can_act, enemies[i].can_act, "
            "enemies[i].targeted or actors[id].uses_command(n)",
            v,
        )
    return params
