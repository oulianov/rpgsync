"""Database tables of RPG_RT.ldb <-> typed Python files.

Every exported table becomes one file holding a list of pydantic models
(``rpgsync/db.py``, generated from liblcf's schema)::

    \"\"\"Enemies - RPG_RT.ldb\"\"\"
    from rpgsync.db import *

    enemies = [
        Enemy(id=1, name="Slime", battler_name="Slime", max_hp=10, attack=8),
        ...
    ]

Files are parsed with :mod:`ast` (never executed), but they are valid Python
so an IDE can type check them against the models.  Only non-default fields
are written; empty slots are left out.

Writing back is a patch: a chunk is only re-encoded when its value changed,
so untouched entries, unexported fields (troop battle events, ...) and
unknown chunks stay byte-for-byte identical.  Like RPG_RT and EasyRPG, the
tables are indexed by position (id N is element N), so they never get holes:
an entry missing from a file is reset to an empty slot, new ids past the end
are appended (gaps are filled with empty slots).
"""

from __future__ import annotations

import ast
import copy
import keyword
import re
from functools import cache
from typing import Any

from pydantic import BaseModel, ValidationError

from . import db as M
from .commands import CompileError, Ctx, TableSize, config_src, pystr, size_stmt, split_size, table_length
from .dbschema import ENUMS, FLAGS, INDEXED, MODEL_STRUCTS, STRUCTS, FieldDef
from .dbschema import TABLES as _TABLE_INFO
from .lcf import ArrayItem, IntVector, LcfError, Struct

#: Exported tables, in file order.  Tables missing from a database (classes
#: and battler animations in RPG Maker 2000) are skipped.
TABLES: list[str] = list(_TABLE_INFO)

#: Model classes that may appear in database files.
MODEL_CLASSES: dict[str, type] = {name: getattr(M, name) for name in MODEL_STRUCTS}


def _camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_"))


MODEL_CLASSES.update({_camel(fl): getattr(M, _camel(fl)) for fl in FLAGS if hasattr(M, _camel(fl))})

HEADER_NOTE = """\
# Edit freely: changes are written back to {target} by rpgsync.
# Every attribute of the class is an entry, in any order; its id is its slot
# in the editor. An entry without id gets the next free slot, a removed entry
# leaves an empty slot. Attribute names are yours: rename them with your IDE."""


# class of each table file: (class name, base class in rpgsync.tables)
TABLE_CLASSES = {
    "variables": ("Variables", "VariableTable"),
    "switches": ("Switches", "SwitchTable"),
    "battleranimations": ("BattlerAnimations", "Table"),
}
# entries written Model(id, "name") instead of Model(id=..., name=...)
POSITIONAL = ("Variable", "Switch")

WIDTH = 120


def _flags_class(name: str) -> type:
    return MODEL_CLASSES[_camel(name)]


def table_struct(name: str) -> str:
    """Model / LCF structure name of a table ("enemies" -> "Enemy")."""
    return _TABLE_INFO[name][1]


def db_engine(db: Struct) -> str:
    """ "2k3" when the database has RPG Maker 2003 chunks, else "2k"."""
    return "2k3" if (db._chunk(0x1D) is not None or db._chunk(0x1E) is not None) else "2k"


def has_table(db: Struct, name: str) -> bool:
    return db._chunk(_TABLE_INFO[name][0]) is not None


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


@cache
def _fields(sname: str) -> tuple[FieldDef, ...]:
    """Exported fields of a structure (no size chunks, no opaque fields)."""
    return tuple(f for f in STRUCTS[sname] if f.size_of is None and f.model != "raw")


@cache
def _class_default(sname: str, fname: str) -> Any:
    return MODEL_CLASSES[sname].model_fields[fname].get_default(call_default_factory=True)


def _engine_dependent(f: FieldDef) -> bool:
    return f.default != f.default_2k3


def _plain(v: Any) -> Any:
    """Comparable value: models become dicts (ignores private attributes)."""
    if isinstance(v, BaseModel):
        return v.model_dump()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _enum_name(enum: str, v: int):
    for k, x in ENUMS[enum].items():
        if x == v:
            return k
    return v


def _enum_value(enum: str, v) -> int:
    if isinstance(v, str):
        if v not in ENUMS[enum]:
            raise ValueError("%r is not one of %s" % (v, ", ".join(ENUMS[enum])))
        return ENUMS[enum][v]
    return v


def _check_range(v: int, lo: int, hi: int, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError("%s value %r out of range %d..%d" % (what, v, lo, hi))
    return v


INT32 = (-0x80000000, 0x7FFFFFFF)
RANGES = {"u8": (0, 255, "byte"), "i16": (-0x8000, 0x7FFF, "16 bit"), "i32": INT32 + ("32 bit",)}
PARAMS = ("maxhp", "maxsp", "attack", "defense", "spirit", "agility")
EQUIP = ("weapon_id", "shield_id", "armor_id", "helmet_id", "accessory_id")


# Values the engines cannot use: (lowest, highest) per engine, None = unbounded.
# Upper bounds are the engine caps (EasyRPG Game_Constants); values above
# them are clamped by the engine, so they are only reported when they can
# never be reached.
_STAT = {"2k": (1, 999), "2k3": (1, 999)}
LIMITS: dict[str, dict[str, dict[str, tuple[int | None, int | None]]]] = {
    "Enemy": {
        "max_hp": {"*": (1, None)},
        "max_sp": {"*": (0, None)},
        **{k: {"*": (0, None)} for k in ("attack", "defense", "spirit", "agility", "exp", "gold")},
        "drop_prob": {"*": (0, 100)},
        "critical_hit_chance": {"*": (1, None)},
        "battler_hue": {"*": (0, 360)},
    },
    "Actor": {
        "initial_level": {"2k": (1, 50), "2k3": (1, 99)},
        "final_level": {"2k": (1, 50), "2k3": (1, 99)},
        "character_index": {"*": (0, 7)},
        "face_index": {"*": (0, 15)},
        "critical_hit_chance": {"*": (1, None)},
    },
    "Item": {
        **{k: {"*": (0, None)} for k in ("price", "uses", "sp_cost")},
        "hit": {"*": (0, 100)},
        "critical_hit": {"*": (0, 100)},
    },
    "Skill": {"sp_cost": {"*": (0, None)}},
    "Parameters": {
        "maxhp": {"2k": (1, 999), "2k3": (1, 9999)},
        "maxsp": {"*": (0, 999)},
        **{k: _STAT for k in ("attack", "defense", "spirit", "agility")},
    },
}


def _check_limits(model: BaseModel, sname: str, engine: str, node: ast.AST) -> None:
    for fname, by_engine in LIMITS.get(sname, {}).items():
        lo, hi = by_engine.get(engine) or by_engine.get("*") or (None, None)
        v = getattr(model, fname, None)
        for x in v if isinstance(v, list) else [v]:
            if not isinstance(x, int) or isinstance(x, bool):
                continue
            if (lo is not None and x < lo) or (hi is not None and x > hi):
                bounds = "%s..%s" % (lo, hi) if hi is not None else "at least %d" % lo
                raise CompileError("%s(): %s: %d is out of range (%s)" % (sname, fname, x, bounds), node)
    for f in type(model).model_fields:
        sub = getattr(model, f, None)
        if isinstance(sub, BaseModel) and type(sub).__name__ in LIMITS:
            _check_limits(sub, type(sub).__name__, engine, node)


# --------------------------------------------------------------------------
# LCF value <-> model value
# --------------------------------------------------------------------------


def _to_model(f: FieldDef, v: Any, ctx: Ctx) -> Any:
    m = f.model
    if m == "int":
        return v
    if m == "bool":
        return bool(v)
    if m == "str":
        return ctx.dec(bytes(v))
    if m.startswith("enum:"):
        return _enum_name(m[5:], v)
    if m == "bits":
        return [bool(x) for x in v]
    if m in RANGES:
        return list(v)
    if m == "params":
        n = len(v) // 6
        return M.Parameters.model_construct(**{k: list(v[i * n : (i + 1) * n]) for i, k in enumerate(PARAMS)})
    if m == "equip":
        vals = list(v[:5]) + [0] * (5 - len(v[:5]))
        return M.Equipment.model_construct(**dict(zip(EQUIP, vals)))
    if m.startswith("flags:"):
        out = {}
        for i, (name, _) in enumerate(FLAGS[m[6:]]):
            out[name] = bool(i // 8 < len(v) and v[i // 8] >> (i % 8) & 1)
        return _flags_class(m[6:]).model_construct(**out)
    if m.startswith("struct:"):
        return struct_to_model(v, m[7:], ctx)
    if m.startswith("array:"):
        return [struct_to_model(it.struct, m[6:], ctx, it.id) for it in v]
    raise LcfError("no model conversion for %s" % m)


def _to_lcf(f: FieldDef, v: Any, ctx: Ctx, cur: Any = None) -> Any:
    """Model value -> LCF value.  `cur` is the current LCF value (or None);
    bits that the model does not represent are taken from it."""
    m = f.model
    if m == "int":
        return _check_range(v, *INT32, f.name)
    if m == "bool":
        if cur is not None and bool(cur) == bool(v):
            return cur
        return bool(v)
    if m == "str":
        return ctx.enc(v)
    if m.startswith("enum:"):
        return _check_range(_enum_value(m[5:], v), *INT32, f.name)
    if m == "bits":
        out = []
        for i, b in enumerate(v):
            if cur is not None and i < len(cur) and bool(cur[i]) == bool(b):
                out.append(cur[i])
            else:
                out.append(1 if b else 0)
        return IntVector(out)
    if m in RANGES:
        lo, hi, what = RANGES[m]
        return IntVector(_check_range(x, lo, hi, "%s (%s)" % (f.name, what)) for x in v)
    if m == "params":
        lists = [getattr(v, k) for k in PARAMS]
        if len({len(x) for x in lists}) > 1:
            raise ValueError(
                "%s: maxhp, maxsp, attack, defense, spirit and agility need the same length "
                "(one value per level)" % f.name
            )
        return IntVector(_check_range(x, -0x8000, 0x7FFF, "%s (16 bit)" % f.name) for x in sum(lists, []))
    if m == "equip":
        out = IntVector(_check_range(getattr(v, k), -0x8000, 0x7FFF, "%s (16 bit)" % f.name) for k in EQUIP)
        if cur is not None and len(cur) > 5:
            out.extend(cur[5:])
        if cur is not None:
            out.trailer = getattr(cur, "trailer", b"")
        return out
    if m.startswith("flags:"):
        flags = FLAGS[m[6:]]
        data = list(cur) if cur is not None else []
        need = (len(flags) + 7) // 8
        data += [0] * (need - len(data))
        for i, (name, _) in enumerate(flags):
            if getattr(v, name):
                data[i // 8] |= 1 << (i % 8)
            else:
                data[i // 8] &= ~(1 << (i % 8)) & 0xFF
        return IntVector(data)
    raise LcfError("no LCF conversion for %s" % m)


def _cmp(f: FieldDef, v: Any, ctx: Ctx) -> Any:
    """Value used to decide whether a field changed."""
    if f.model == "str":
        return ctx.enc(v)
    if f.model.startswith("enum:") and isinstance(v, str):
        return ENUMS[f.model[5:]].get(v, v)
    return _plain(v)


def struct_to_model(st: Struct, sname: str, ctx: Ctx, id: int | None = None) -> M.DbModel:
    """Decode a structure into its model (absent chunks get the model defaults)."""
    vals: dict[str, Any] = {}
    if sname in INDEXED:
        vals["id"] = id
    for f in _fields(sname):
        if st._chunk(f.id) is None:
            vals[f.name] = _class_default(sname, f.name)
        else:
            vals[f.name] = _to_model(f, st.get(f.id), ctx)
    return MODEL_CLASSES[sname].model_construct(**vals)


# --------------------------------------------------------------------------
# New entries
# --------------------------------------------------------------------------


def new_struct(sname: str, engine: str, actors: int = 0) -> Struct:
    """An empty entry laid out like the RPG Maker editor writes one: the
    chunks liblcf marks PersistIfDefault, with their default values, plus the
    editor's initial stat curves / battle commands / battle animations.
    `actors` is the number of actors (a new 2003 skill gets one battle
    animation setting per actor)."""
    st = Struct(sname)
    for f in STRUCTS[sname]:
        if f.id is None or f.size_of is not None or not f.persist:
            continue
        if f.is2k3 and engine != "2k3":
            continue
        value = _default_lcf(f, engine)
        if value is not None:
            st.set(f.id, value)
    if sname == "Actor":
        levels = 99 if engine == "2k3" else 50
        st.set("parameters", IntVector([1] * levels + [0] * levels + [1] * (4 * levels)))
        if engine == "2k3":
            st.set("battle_commands", IntVector([-1] * 7))
    if sname == "Item" and engine == "2k3":
        st.set("animation_data", [ArrayItem(id=1, struct=new_struct("BattlerAnimationItemSkill", engine))])
    if sname == "Skill" and engine == "2k3":
        st.set(
            "battler_animation_data",
            [ArrayItem(id=n, struct=new_struct("BattlerAnimationItemSkill", engine)) for n in range(1, actors + 1)],
        )
    return st


def _default_lcf(f: FieldDef, engine: str) -> Any:
    d = f.default_2k3 if engine == "2k3" else f.default
    if f.kind in ("int", "bool"):
        if f.model.startswith("enum:"):
            return _enum_value(f.model[5:], d)
        return d if d is not None else 0
    if f.kind == "str":
        return (d or "").encode("ascii")
    if f.model == "equip":
        return IntVector([0] * 5)
    if f.kind in ("i16vec", "i32vec", "u8vec"):
        return IntVector()
    if f.kind.startswith("S:"):
        return new_struct(f.kind[2:], engine)
    if f.kind.startswith("A:"):
        return []
    if f.kind == "raw" and f.type.startswith("Array<"):
        return b"\x00"  # empty opaque array (e.g. troop pages)
    return None


@cache
def _new_model_plain(sname: str, engine: str) -> dict[str, Any]:
    return struct_to_model(new_struct(sname, engine), sname, Ctx(encoding="ascii")).model_dump()


def _is_default_element(e: M.DbModel) -> bool:
    return e.model_dump(exclude={"id"}) == type(e)().model_dump(exclude={"id"})


def is_empty_entry(model: M.DbModel, sname: str, engine: str) -> bool:
    """An unused slot: every field is the default, or what the editor writes
    for a new entry (arrays of default elements count as empty)."""
    fresh = _new_model_plain(sname, engine)
    for f in _fields(sname):
        v = _plain(getattr(model, f.name))
        if v == _plain(_class_default(sname, f.name)) or v == fresh[f.name]:
            continue
        if f.model.startswith("array:") and all(_is_default_element(e) for e in getattr(model, f.name)):
            continue
        return False
    return True


# --------------------------------------------------------------------------
# Patching
# --------------------------------------------------------------------------


def patch_struct(
    st: Struct, sname: str, model: M.DbModel, ctx: Ctx, engine: str, keep_new_defaults: bool = False
) -> bool:
    """Make `st` carry `model`, re-encoding only the chunks whose value
    changed.  Returns True when something changed.

    With `keep_new_defaults` (filling an empty slot), fields left at their
    model default keep the value of the empty slot, so a new actor written
    as ``Actor(id=9, name="Bob")`` gets the editor's initial stat curves."""
    changed = False
    for f in _fields(sname):
        target = getattr(model, f.name)
        if keep_new_defaults and _plain(target) == _plain(_class_default(sname, f.name)):
            continue
        present = st._chunk(f.id) is not None
        m = f.model
        if m.startswith("struct:"):
            sub = st.get(f.id) if present else new_struct(m[7:], engine)
            if patch_struct(sub, m[7:], target, ctx, engine):
                st.set(f.id, sub)
                changed = True
        elif m.startswith("array:"):
            items = list(st.get(f.id)) if present else []
            new, ch = _patch_array(items, m[6:], target, ctx, engine)
            if ch:
                st.set(f.id, new)
                changed = True
        elif _engine_dependent(f) and target is None:
            if present:
                st.remove(f.id)
                changed = True
        else:
            cur = st.get(f.id) if present else None
            cur_model = _to_model(f, cur, ctx) if present else _class_default(sname, f.name)
            if cur_model is None or _cmp(f, cur_model, ctx) != _cmp(f, target, ctx):
                st.set(f.id, _to_lcf(f, target, ctx, cur))
                changed = True
    return changed


def _patch_array(
    items: list[ArrayItem], ename: str, targets: list[M.DbModel], ctx: Ctx, engine: str
) -> tuple[list[ArrayItem], bool]:
    """Patch array elements by position."""
    changed = len(items) != len(targets)
    out = []
    for i, t in enumerate(targets):
        tid = t.id if getattr(t, "id", None) is not None else i + 1
        if i < len(items):
            it = items[i]
            if it.id != tid:
                it.id = tid
                changed = True
        else:
            it = ArrayItem(id=tid, struct=new_struct(ename, engine))
            changed = True
        if patch_struct(it.struct, ename, t, ctx, engine):
            changed = True
        out.append(it)
    return out, changed


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def table_entries_from_bin(db: Struct, name: str, ctx: Ctx) -> list[M.DbModel]:
    """The used entries of a table (empty slots are not listed)."""
    if not has_table(db, name):
        return []
    sname = table_struct(name)
    engine = db_engine(db)
    out = []
    for it in db.get(name):
        model = struct_to_model(it.struct, sname, ctx, it.id)
        if not is_empty_entry(model, sname, engine):
            out.append(model)
    return out


def apply_table(db: Struct, name: str, entries: list[M.DbModel], ctx: Ctx) -> dict[str, list[int]]:
    """Patch a table of the database to hold exactly `entries`.

    Entries are looked up by position, so the table never gets holes: an
    entry missing from `entries` is reset to an empty slot, ids past the end
    are appended and gaps are filled with empty slots."""
    summary: dict[str, list[int]] = {"added": [], "removed": [], "changed": []}
    if not has_table(db, name):
        if entries:
            raise ValueError("this database has no %s table (RPG Maker 2003 only)" % name)
        return summary
    entries, wanted = split_size(entries)
    sname = table_struct(name)
    engine = db_engine(db)
    actors = len(db.get("actors")) if has_table(db, "actors") else 0
    current = list(db.get(name))
    if any(it.id != n for n, it in enumerate(current, 1)):
        raise ValueError("the database's %s are not numbered 1..N; refusing to modify them" % name)
    by_id: dict[int, M.DbModel] = {}
    for e in entries:
        if not isinstance(e.id, int) or isinstance(e.id, bool) or e.id < 1:
            raise ValueError("%s: id must be a positive integer, got %r" % (name, e.id))
        if e.id in by_id:
            raise ValueError("%s: id %d is used twice" % (name, e.id))
        by_id[e.id] = e
    size = table_length(len(current), wanted, list(by_id))
    items = []
    for n in range(1, size + 1):
        item = current[n - 1] if n <= len(current) else None
        spec = by_id.get(n)
        if item is None:
            item = ArrayItem(id=n, struct=new_struct(sname, engine, actors))
            if spec is not None:
                patch_struct(item.struct, sname, spec, ctx, engine, keep_new_defaults=True)
                summary["added"].append(n)
        else:
            was_empty = is_empty_entry(struct_to_model(item.struct, sname, ctx, n), sname, engine)
            if spec is None:
                if not was_empty:
                    item = ArrayItem(id=n, struct=new_struct(sname, engine, actors))
                    summary["removed"].append(n)
            elif patch_struct(item.struct, sname, spec, ctx, engine, keep_new_defaults=was_empty):
                summary["added" if was_empty else "changed"].append(n)
        items.append(item)
    if size != len(current):
        summary["size"] = [len(current), size]
    if summary["added"] or summary["removed"] or summary["changed"] or len(items) != len(current):
        db.set(name, items)
    return summary


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
#
# Values are rendered through a tiny layout tree so that long entries can be
# broken over several lines:  ("atom", text) | ("call", name, [(kw, node)])
# | ("list", [node]) | ("mul", node, n)


def _atom(v: Any) -> tuple:
    if isinstance(v, str):
        return ("atom", pystr(v))
    return ("atom", repr(v))


def _list_node(nodes: list[tuple]) -> tuple:
    if len(nodes) >= 3 and all(n == nodes[0] for n in nodes):
        return ("mul", ("list", [nodes[0]]), len(nodes))
    return ("list", nodes)


def _model_node(model: BaseModel, sname: str, position: int | None = None, top: bool = False) -> tuple:
    kws = []
    if sname in INDEXED and (top or getattr(model, "id", None) not in (None, position)):
        kws.append(("id", _atom(model.id)))
    if sname in STRUCTS and sname not in ("Parameters", "Equipment"):
        for f in _fields(sname):
            v = getattr(model, f.name)
            if _plain(v) == _plain(_class_default(sname, f.name)):
                continue
            kws.append((f.name, _value_node(f, v)))
    else:  # packed structures and flags: plain fields
        defaults = type(model)()
        for k in type(model).model_fields:
            v = getattr(model, k)
            if v != getattr(defaults, k):
                kws.append((k, _list_node([_atom(x) for x in v]) if isinstance(v, list) else _atom(v)))
    return ("call", type(model).__name__, kws)


def _value_node(f: FieldDef, v: Any) -> tuple:
    m = f.model
    if m.startswith("struct:"):
        return _model_node(v, m[7:])
    if m in ("params", "equip") or m.startswith("flags:"):
        return _model_node(v, type(v).__name__)
    if m.startswith("array:"):
        ename = m[6:]
        nodes = [_model_node(e, ename, i + 1) for i, e in enumerate(v)]
        positional = all(getattr(e, "id", None) == i + 1 for i, e in enumerate(v))
        if positional and len(nodes) >= 2 and all(n == nodes[0] for n in nodes):
            return ("mul", ("list", [nodes[0]]), len(nodes))
        return ("list", nodes)
    if isinstance(v, list):
        return _list_node([_atom(x) for x in v])
    return _atom(v)


def _flat(n: tuple) -> str:
    if n[0] == "atom":
        return n[1]
    if n[0] == "call":
        return "%s(%s)" % (n[1], ", ".join("%s=%s" % (k, _flat(v)) for k, v in n[2]))
    if n[0] == "list":
        return "[%s]" % ", ".join(_flat(x) for x in n[1])
    return "%s * %d" % (_flat(n[1]), n[2])


def _fmt(n: tuple, ind: int, col: int, tail: int = 1) -> str:
    """Render node `n` starting at column `col` on a line indented by `ind`;
    `tail` characters follow it on its last line."""
    flat = _flat(n)
    if col + len(flat) + tail <= WIDTH or n[0] == "atom" or _is_row(n):
        return flat
    pad = " " * (ind + 4)
    if n[0] == "mul":
        return "%s * %d" % (_fmt(n[1], ind, col, tail + len(" * %d" % n[2])), n[2])
    if n[0] == "call":
        if not n[2]:
            return flat
        lines = ["%s(" % n[1]]
        for k, v in n[2]:
            lines.append("%s%s=%s," % (pad, k, _fmt(v, ind + 4, ind + 4 + len(k) + 1)))
        lines.append(" " * ind + ")")
        return "\n".join(lines)
    lines = ["["]
    for x in n[1]:
        lines.append("%s%s," % (pad, _fmt(x, ind + 4, ind + 4)))
    lines.append(" " * ind + "]")
    return "\n".join(lines)


def _is_row(n: tuple) -> bool:
    """Lists of plain values (stat curves, flags, ...) stay on one line,
    however long: one row per field reads better than one value per line."""
    if n[0] == "mul":
        return _is_row(n[1])
    return n[0] == "list" and all(x[0] == "atom" for x in n[1])


def table_class(name: str) -> tuple[str, str]:
    return TABLE_CLASSES.get(name, (_camel(name), "Table"))


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def identifiers(name: str, names: dict[int, str], keep: dict[int, str] | None = None) -> dict[int, str]:
    """Attribute names of a table's entries: the ones already in the file
    (`keep`), else derived from the entry names ("Code voulu" -> code_voulu),
    else <entry>_<id>."""
    from . import dsl
    from .script import _slug

    base = {"VariableTable": dsl.Variables, "SwitchTable": dsl.Switches}.get(table_class(name)[1], object)
    reserved = set(dir(base)) | set(keyword.kwlist) | {"size", "Config"}
    singular = _snake(table_struct(name))
    out: dict[int, str] = {}
    used: set[str] = set()
    for n, ident in (keep or {}).items():
        if n in names and ident.isidentifier() and ident not in reserved and ident not in used:
            out[n] = ident
            used.add(ident)
    for n, entry_name in names.items():
        if n in out:
            continue
        slug = _slug(entry_name)
        if not slug or slug[0].isdigit() or slug in reserved:
            slug = "%s_%d" % (singular, n)
        if slug in used:
            slug = base = "%s_%d" % (slug, n)
            k = 2
            while slug in used:  # "Potion", "Potion 3", "Potion" (id 3): potion_3 is taken too
                slug = "%s_%d" % (base, k)
                k += 1
        out[n] = slug
        used.add(slug)
    return out


def read_identifiers(text: str) -> dict[int, str]:
    """id -> attribute name of the entries of a database file (best effort,
    for keeping the names when the file is rewritten)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    out = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                v = stmt.value
                if isinstance(v, ast.Call):
                    first = v.args[0] if v.args else next((k.value for k in v.keywords if k.arg == "id"), None)
                    if isinstance(first, ast.Constant) and type(first.value) is int:
                        out[first.value] = stmt.targets[0].id
    return out


def _entry_src(e: M.DbModel, sname: str, ident: str, skip_width: int | None = None) -> str:
    if sname in POSITIONAL:
        return "%s = %s(%d, %s)" % (ident, sname, e.id, pystr(e.name))  # type: ignore[attr-defined]
    src = "%s = %s" % (ident, _fmt(_model_node(e, sname, top=True), 0, 4 + len(ident) + 3, 0))
    if skip_width is not None and any(len(line) + 4 > skip_width for line in src.split("\n")):
        src += "  # fmt: skip"  # keep the long lists on one line each
    return src


def render_table(
    name: str,
    entries: list[M.DbModel],
    target: str = "RPG_RT.ldb",
    size: int | None = None,
    keep: dict[int, str] | None = None,
    skip_width: int | None = None,
) -> str:
    """A database file: one class whose attributes are the entries.
    `keep`: attribute names to reuse (id -> name), from the current file."""
    sname = table_struct(name)
    cls, base = table_class(name)
    idents = identifiers(name, {e.id: getattr(e, "name", "") or "" for e in entries if e.id is not None}, keep)
    out = [
        '"""%s - %s"""' % (_TABLE_INFO[name][2], target),
        "",
        HEADER_NOTE.format(target=target),
        "from rpgsync.tables import *",
        "",
        "",
        "class %s(%s):" % (cls, base),
    ]
    body = []
    if size is not None:
        body += config_src(size, "")
        if entries:
            body.append("")
    for e in entries:
        body.append(_entry_src(e, sname, idents[e.id], skip_width))  # type: ignore[index]
    if not body:
        body = ["pass"]
    out += ["    " + line if line else "" for line in "\n".join(body).split("\n")]
    out += ["", "", "%s = %s()" % (name, cls)]
    return "\n".join(out) + "\n"


def export_tables(
    db: Struct,
    ctx: Ctx,
    target: str = "RPG_RT.ldb",
    keep: dict[str, dict[int, str]] | None = None,
    skip_width: int | None = None,
    tables: list[str] | None = None,
) -> dict[str, str]:
    """Table name -> file text, for every exported table present in `db`
    (or only `tables`).  `keep`: per table, the attribute names to reuse."""
    return {
        name: render_table(
            name, table_entries_from_bin(db, name, ctx), target, len(db.get(name)), (keep or {}).get(name), skip_width
        )
        for name in TABLES
        if has_table(db, name) and (tables is None or name in tables)
    }


# --------------------------------------------------------------------------
# Compile
# --------------------------------------------------------------------------


def _validation_message(cls_name: str, e: ValidationError) -> str:
    parts = []
    seen = set()
    for err in e.errors():
        # a field typed `Literal[...] | int` reports one error per branch: keep the first
        loc = ".".join(str(x) for x in err.get("loc", ()) if not str(x).startswith(("literal[", "int", "str")))
        if loc in seen:
            continue
        seen.add(loc)
        msg = err.get("msg", "invalid value")
        if err.get("type") == "extra_forbidden":
            msg = "unknown field"
        parts.append("%s: %s" % (loc, msg) if loc else msg)
    return "%s(): %s" % (cls_name, "; ".join(parts))


def _eval(node: ast.AST) -> Any:
    """Evaluate a literal database value: constants, lists, model calls."""
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (bool, int, str)):
            return node.value
        raise CompileError("unsupported value %r" % (node.value,), node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        if isinstance(v, bool) or not isinstance(v, int):
            raise CompileError("unary minus needs a number", node)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            if isinstance(e, ast.Starred):
                raise CompileError("*unpacking is not supported", e)
            out.append(_eval(e))
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(a, int) and isinstance(b, list):
            a, b = b, a
        if not isinstance(a, list) or isinstance(b, bool) or not isinstance(b, int) or b < 0:
            raise CompileError("only `[...] * count` is supported", node)
        return [copy.deepcopy(x) for _ in range(b) for x in a]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in MODEL_CLASSES:
            raise CompileError(
                "unknown call %s(); expected one of the rpgsync.db models" % ast.unparse(node.func), node
            )
        cls = MODEL_CLASSES[node.func.id]
        positional = [f for f in ("id", "name") if f in cls.model_fields]
        if len(node.args) > len(positional) or any(isinstance(a, ast.Starred) for a in node.args):
            raise CompileError(
                "%s(): only %s can be given without a keyword" % (node.func.id, " and ".join(positional) or "nothing"),
                node,
            )
        kw = {f: _eval(a) for f, a in zip(positional, node.args)}
        for k in node.keywords:
            if k.arg is None:
                raise CompileError("%s(): **kwargs is not supported" % node.func.id, node)
            if k.arg in kw:
                raise CompileError("%s(): %s given twice" % (node.func.id, k.arg), k.value)
            kw[k.arg] = _eval(k.value)
        try:
            return cls(**kw)
        except ValidationError as e:
            raise CompileError(_validation_message(node.func.id, e), node) from None
    if isinstance(node, ast.Name):
        raise CompileError("unknown name %r (only literal values are allowed)" % node.id, node)
    raise CompileError("unsupported expression %s" % ast.unparse(node), node)


def _normalize(model: BaseModel, sname: str) -> None:
    """Enum numbers -> names, element ids -> explicit positions."""
    for f in _fields(sname):
        v = getattr(model, f.name)
        m = f.model
        if m.startswith("enum:") and isinstance(v, int) and not isinstance(v, bool):
            setattr(model, f.name, _enum_name(m[5:], v))
        elif m.startswith("struct:"):
            _normalize(v, m[7:])
        elif m.startswith("array:"):
            for i, e in enumerate(v):
                if e.id is None:
                    e.id = i + 1
                _normalize(e, m[6:])


def _check_encodable(model: BaseModel, sname: str, ctx: Ctx, node: ast.AST) -> None:
    """Fail at compile time for values that cannot be stored (text outside
    the game encoding, numbers out of range...)."""
    for f in _fields(sname):
        v = getattr(model, f.name)
        m = f.model
        try:
            if m.startswith("struct:"):
                _check_encodable(v, m[7:], ctx, node)
            elif m.startswith("array:"):
                for e in v:
                    _check_range(e.id, *INT32, "%s id" % f.name)
                    _check_encodable(e, m[6:], ctx, node)
            elif not (_engine_dependent(f) and v is None):
                _to_lcf(f, v, ctx)
        except CompileError as e:
            raise CompileError("%s: %s" % (f.name, e), node) from None
        except (ValueError, LcfError) as e:
            raise CompileError("%s: %s" % (type(model).__name__, e), node) from None


def _table_body(name: str, tree: ast.Module) -> tuple[list[ast.stmt], ast.List | None]:
    """Statements holding the entries: the class body (or, in files written
    by older rpgsync versions, the top level with a `name = [...]` list)."""
    classes = [st for st in tree.body if isinstance(st, ast.ClassDef)]
    if len(classes) > 1:
        raise CompileError("a database file holds one class", classes[1])
    legacy = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.ClassDef)) or size_stmt(stmt) is not None:
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            v = stmt.value
            if classes and isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == classes[0].name:
                continue  # `variables = Variables()`
            if not classes and stmt.targets[0].id == name and isinstance(v, ast.List):
                legacy = v
                continue
        cls_name = table_class(name)[0]
        raise CompileError(
            "top level may only contain `class %s(...)` and `%s = %s()`" % (cls_name, name, cls_name), stmt
        )
    if classes:
        return classes[0].body, None
    if legacy is None:
        # not an empty table (that is `class X(Table): pass`): a truncated or
        # emptied file must not wipe the whole table
        cls_name, base = table_class(name)
        raise CompileError("missing `class %s(%s):` holding the entries" % (cls_name, base))
    return [st for st in tree.body if size_stmt(st) is not None], legacy


def table_size(text: str) -> TableSize | None:
    """The ``size = N`` statement of a database file (checked by compile_table)."""
    tree = ast.parse(text)
    classes = [st for st in tree.body if isinstance(st, ast.ClassDef)]
    for stmt in classes[0].body if classes else tree.body:
        size = size_stmt(stmt)
        if size is not None:
            return size
    return None


def compile_table(name: str, text: str, ctx: Ctx, filename: str = "<database>") -> list[M.DbModel]:
    """Parse a database file into its entries (validated models, with their
    attribute name in ``_ident``).  ``size = N`` is read by table_size()."""
    if name not in _TABLE_INFO:
        raise CompileError("unknown database table %r" % name)
    sname = table_struct(name)
    cls = MODEL_CLASSES[sname]
    try:
        tree = ast.parse(text, filename)
    except SyntaxError as e:
        raise CompileError("syntax error: %s" % e.msg, e) from None
    body, legacy = _table_body(name, tree)
    items: list[tuple[str | None, ast.expr]] = []
    seen_size = False
    names: dict[str, int] = {}
    for stmt in body:
        if isinstance(stmt, ast.Pass) or (
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)
        ):
            continue
        if size_stmt(stmt) is not None:
            if seen_size:
                raise CompileError("size is assigned twice", stmt)
            seen_size = True
            continue
        target = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target = stmt.target
        if not isinstance(target, ast.Name) or stmt.value is None:  # type: ignore[union-attr]
            raise CompileError("a database class may only contain `size = N` and `name = %s(...)`" % sname, stmt)
        if target.id in names:
            raise CompileError("%s is defined twice (also on line %d)" % (target.id, names[target.id]), stmt)
        names[target.id] = stmt.lineno
        items.append((target.id, stmt.value))  # type: ignore[union-attr]
    if legacy is not None:
        items += [(None, node) for node in legacy.elts]
    entries = []
    seen: dict[int, int] = {}
    for ident, node in items:
        entry = _eval(node)
        if not isinstance(entry, cls):
            raise CompileError("%s entries must be %s(...)" % (name, sname), node)
        if entry.id is not None and (isinstance(entry.id, bool) or entry.id < 1):
            raise CompileError("%s(): id must be a positive integer" % sname, node)
        if entry.id is not None and entry.id in seen:
            raise CompileError("%s id %d is used twice (also on line %d)" % (sname, entry.id, seen[entry.id]), node)
        if entry.id is not None:
            seen[entry.id] = node.lineno
        _normalize(entry, sname)
        _check_limits(entry, sname, ctx.engine, node)
        _check_encodable(entry, sname, ctx, node)
        entry._lineno = node.lineno
        entry._ident = ident
        entries.append(entry)
    return entries
