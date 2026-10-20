"""The map tree of RPG_RT.lmt <-> Scripts/database/map_tree.py.

The file is one class whose attributes are the maps and areas of the
editor's map tree, plus ``start``, where a new game begins.  The maps under a
map are in the ``with`` block that follows it::

    class Maps(MapTree):
        start = Start(party_map_id=1, party_x=8, party_y=6)

        hertepay = Map(id=5, name="Hertepay", music_type="event")
        with hertepay:
            relm = Map(id=1, name="Relm", encounters=[Encounter(troop_id=3)])
            with relm:
                cellar = Area(id=26, name="Cellar", area_rect=[2, 3, 9, 8])


    maps = Maps()

What a ``with`` block of a class body defines is still an attribute of the
class: event scripts name every map by its attribute, wherever it is in the
tree: ``teleport(maps.cellar, 10, 4)``.  The order of the entries is the order
of the tree; the editor's own state (scroll positions, expanded nodes) is left
alone.  A new ``Map(...)`` creates an empty map file, a removed one deletes its
map file and its script (both are backed up in .rpgsync/history first).  The
root of the tree (the game's title, id 0) is not listed.

``parent_map=`` is only written for a map whose parent is not listed (a broken
tree).  A flat file of an older rpgsync (``parent_map=`` on every map) is still
read, and written back nested.

Like the database files, the file is parsed with :mod:`ast` (never run).
"""

from __future__ import annotations

import ast
import keyword
import os
import re

from pydantic import BaseModel

from . import db as M
from .commands import CompileError, Ctx
from .database import (
    _check_encodable,
    _eval,
    _fmt,
    _model_node,
    _normalize,
    new_struct,
    patch_struct,
    struct_to_model,
)
from .lcf import ArrayItem, IntVector, LcfError, LcfFile, MapTree, Reader, Struct, ber

TARGET = "RPG_RT.lmt"
CLASS = "Maps"
BASE = "MapTree"
# entry classes of the file -> MapInfo type
ENTRY_TYPES = {"Map": "map", "Area": "area"}
RESERVED = {"start", "Config", "size"}
# MapInfo chunks of the editor's own state: not the game's, not exported
EDITOR_ONLY = {0x03, 0x05, 0x06, 0x07}
INDENTATION = 0x03

HEADER_NOTE = """\
# Edit freely: changes are written back to RPG_RT.lmt by rpgsync.
# Every attribute of the class is a map or an area of the editor's map tree, in
# tree order: the maps under a map are in the `with` block that follows it.
# Event scripts name maps by their attribute: teleport(maps.relm, 10, 4).
# A new Map(...) creates an empty map; removing one deletes the map with its
# script (backed up in .rpgsync/history). Attribute names are yours: rename them
# with your IDE."""


class TreeOrder(BaseModel):
    """The order of the map tree: map ids as the editor lists them (without the root).
    Travels with the entries as a pseudo-entry of id 0, so that it is merged like them."""

    id: int = 0
    order: list[int]
    lineno: int | None = None
    # read from the file: maps under a new map (attribute -> its parent's), until it has an id
    pending: dict[str, str] = {}
    # read from the file: flat, with parent_map=, as an older rpgsync wrote it
    flat: bool = False

    def key(self) -> tuple:
        return ("order", tuple(self.order))


class StartSpec(BaseModel):
    """``start = Start(...)``: where a new game begins (pseudo-entry of id -1)."""

    id: int = -1
    start: M.Start
    lineno: int | None = None

    def key(self) -> tuple:
        return ("start", self.start.key())


# --------------------------------------------------------------------------
# Tree helpers
# --------------------------------------------------------------------------


def map_type(item: ArrayItem) -> int:
    """0 root, 1 map, 2 area (-1: not set)."""
    c = item.struct._chunk(0x04)
    return Reader(c.raw).ber() if c is not None and c.raw else -1


def is_root(item: ArrayItem) -> bool:
    """The root of the tree (id 0, the game's title)."""
    return item.id == 0 or map_type(item) == 0


def is_kept(item: ArrayItem) -> bool:
    """Not listed in map_tree.py, kept as it is: the root, and entries without a valid
    id (TestGame-2000 tests a map of id -1)."""
    return item.id <= 0 or map_type(item) == 0


def tree_order(parents: dict[int, int], rank: dict[int, int], roots: set[int]) -> list[int]:
    """Map ids in tree order (depth first, children by `rank` then id), without the roots.
    Maps whose parent is missing (or in a cycle) come last, with their children."""
    children: dict[int, list[int]] = {}
    for mid, parent in parents.items():
        children.setdefault(parent, []).append(mid)
    for kids in children.values():
        kids.sort(key=lambda m: (rank.get(m, len(rank)), m))
    out: list[int] = []
    seen: set[int] = set()

    def visit(mid: int) -> None:
        for kid in children.get(mid, []):
            if kid not in seen:
                seen.add(kid)
                out.append(kid)
                visit(kid)

    for root in sorted(roots):
        visit(root)
    for mid in sorted(parents, key=lambda m: (rank.get(m, len(rank)), m)):
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
            visit(mid)
    return out


def _depth(mid: int, parents: dict[int, int]) -> int:
    depth, seen = 0, set()
    while mid in parents and mid not in seen:
        seen.add(mid)
        mid = parents[mid]
        depth += 1
    return depth


# --------------------------------------------------------------------------
# Game -> models
# --------------------------------------------------------------------------


def entries_from_bin(tree: MapTree, ctx: Ctx) -> list[M.MapInfo]:
    """The maps and areas of the tree (not the root), in tree order."""
    items = {it.id: it for it in tree.maps if not is_kept(it)}
    roots = {it.id for it in tree.maps if is_root(it)} or {0}
    parents = {mid: it.struct.get("parent_map") for mid, it in items.items()}
    rank = {mid: n for n, mid in enumerate(tree.tree_order)}
    return [struct_to_model(items[mid].struct, "MapInfo", ctx, mid) for mid in tree_order(parents, rank, roots)]


def specs_from_bin(tree: MapTree, ctx: Ctx) -> list:
    entries = entries_from_bin(tree, ctx)
    start = struct_to_model(tree.start, "Start", ctx)
    return [*entries, TreeOrder(order=[e.id for e in entries]), StartSpec(start=start)]  # type: ignore[arg-type]


def fingerprint(data: bytes) -> bytes:
    """What the script shows of the map tree: without the editor's own state (scroll
    positions, expanded nodes, the selected node), which changes as one browses it."""
    tree = MapTree.parse(data)
    out = bytearray()
    for it in tree.maps:
        out += ber(it.id)
        for c in it.struct.chunks:
            if c.id not in EDITOR_ONLY:
                out += ber(c.id) + ber(len(c.raw or b"")) + (c.raw or b"")
        out += b"\0"
    out += repr(tree.tree_order).encode() + b"\0"
    for c in tree.start.chunks:
        out += ber(c.id) + ber(len(c.raw or b"")) + (c.raw or b"")
    return bytes(out)


# --------------------------------------------------------------------------
# Attribute names
# --------------------------------------------------------------------------


def identifiers(names: dict[int, str], keep: dict[int, str] | None = None) -> dict[int, str]:
    """Attribute names of the maps (id -> name): the ones already in the file (`keep`),
    else derived from the map names ("Relm (1)" -> relm_1), else map_<id>."""
    from .script import _slug

    reserved = RESERVED | set(keyword.kwlist)
    out: dict[int, str] = {}
    used: set[str] = set()
    for n, ident in (keep or {}).items():
        if n in names and ident.isidentifier() and ident not in reserved and ident not in used:
            out[n] = ident
            used.add(ident)
    for n, name in sorted(names.items()):
        if n in out:
            continue
        slug = _slug(name)
        if not slug or slug[0].isdigit() or slug in reserved:
            slug = "map_%d" % n
        if slug in used:
            slug = base = "%s_%d" % (slug, n)
            k = 2
            while slug in used:
                slug = "%s_%d" % (base, k)
                k += 1
        out[n] = slug
        used.add(slug)
    return out


def _target(stmt: ast.stmt) -> ast.Name | None:
    """The name an assignment of the class body defines (None: not ``name = ...``)."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0]
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None and isinstance(stmt.target, ast.Name):
        return stmt.target
    return None


def _assigns(body: list[ast.stmt]):
    """The assignments of a class body, those of its ``with`` blocks included, in order."""
    for stmt in body:
        if isinstance(stmt, ast.With):
            yield from _assigns(stmt.body)
        elif _target(stmt) is not None:
            yield stmt


def read_identifiers(text: str) -> dict[int, str]:
    """id -> attribute name of the entries of map_tree.py (best effort)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    out = {}
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for stmt in _assigns(cls.body):
                v = stmt.value
                if isinstance(v, ast.Call):
                    first = v.args[0] if v.args else next((k.value for k in v.keywords if k.arg == "id"), None)
                    if isinstance(first, ast.Constant) and type(first.value) is int:
                        out[first.value] = _target(stmt).id  # type: ignore[union-attr]
    return out


# --------------------------------------------------------------------------
# Models -> file
# --------------------------------------------------------------------------


def _entry_node(e: M.MapInfo, parent: bool = False) -> tuple:
    """Map(...) / Area(...) of an entry: without its type, nor its parent unless `parent`
    (the parent of an entry is the ``with`` block it is in)."""
    node = _model_node(e, "MapInfo", top=True)
    drop = set() if parent else {"parent_map"}
    kind = next((k for k, t in ENTRY_TYPES.items() if t == e.type), None)
    if kind is not None:
        drop.add("type")  # else a type RPG Maker doesn't make: kept as it is
    return ("call", kind or node[1], [kw for kw in node[2] if kw[0] not in drop])


def _start_comment(start: M.Start, names: dict[int, str]) -> str | None:
    """# New game: the party on Relm; boat, ship on World"""
    on: dict[int, list[str]] = {}
    for who, mid in (
        ("the party", start.party_map_id),
        ("boat", start.boat_map_id),
        ("ship", start.ship_map_id),
        ("airship", start.airship_map_id),
    ):
        if mid:
            on.setdefault(mid, []).append(who)
    if not on:
        return None
    parts = ["%s on %s" % (", ".join(who), names.get(mid, "map %d (missing)" % mid)) for mid, who in on.items()]
    return "# New game: " + "; ".join(parts)


def render_map_tree(entries: list[M.MapInfo], start: M.Start, keep: dict[int, str] | None = None) -> str:
    """map_tree.py for these entries (in tree order) and start positions."""
    names = {e.id: e.name for e in entries if e.id is not None}
    idents = identifiers(names, keep)
    children: dict[int, list[M.MapInfo]] = {}
    for e in entries:
        if e.parent_map in names and e.parent_map != e.id:
            children.setdefault(e.parent_map, []).append(e)
    body = []
    comment = _start_comment(start, names)
    if comment:
        body.append(comment)
    body.append("start = %s" % _fmt(_model_node(start, "Start"), 0, 4 + len("start = "), 0))
    done: set[int] = set()

    def emit(e: M.MapInfo, depth: int, parent: bool) -> None:
        done.add(e.id)  # type: ignore[arg-type]
        ident, pad = idents[e.id], "    " * depth  # type: ignore[index]
        src = "%s = %s" % (ident, _fmt(_entry_node(e, parent), 0, 4 * (depth + 1) + len(ident) + 3, 0))
        body.extend(pad + line for line in src.split("\n"))
        kids = [k for k in children.get(e.id, []) if k.id not in done]  # type: ignore[arg-type]
        if kids:
            body.append("%swith %s:" % (pad, ident))
            for k in kids:
                emit(k, depth + 1, False)

    for e in entries:
        if e.id not in done:
            # the maps of the top level; a map whose parent is not listed (a broken
            # tree) is there too, with its parent_map
            body.append("")
            emit(e, 0, e.parent_map != 0)
    out = [
        '"""Map tree - %s"""' % TARGET,
        "",
        HEADER_NOTE,
        "from rpgsync.tables import *",
        "",
        "",
        "class %s(%s):" % (CLASS, BASE),
    ]
    out += ["    " + line if line else "" for line in "\n".join(body).split("\n")]
    out += ["", "", "maps = %s()" % CLASS]
    return "\n".join(out) + "\n"


def refresh_comments(text: str, data: bytes, ctx: Ctx) -> str:
    """map_tree.py with the comment above ``start`` (``# New game: the party on Relm...``)
    matching the tree written into `data`: after new start positions or a rename.
    The rest of the file is kept."""
    try:
        body = _body(ast.parse(text))
    except (SyntaxError, CompileError):
        return text
    stmt = next((s for s in body if (n := _target(s)) is not None and n.id == "start"), None)
    if stmt is None:
        return text
    tree = MapTree.parse(data)
    names = {e.id: e.name for e in entries_from_bin(tree, ctx) if e.id is not None}
    comment = _start_comment(struct_to_model(tree.start, "Start", ctx), names)  # type: ignore[arg-type]
    lines = text.split("\n")
    index = stmt.lineno - 1
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    top = index  # the comments right above start: the generated one, notes of the user
    while top > 0 and lines[top - 1].lstrip().startswith("#"):
        top -= 1
    found = next((i for i in range(top, index) if lines[i].strip().startswith("# New game: ")), None)
    if found is not None and comment is not None:
        lines[found] = indent + comment
    elif found is not None:
        del lines[found]
    elif comment is not None:
        lines.insert(top, indent + comment)
    return "\n".join(lines)


def decompile_map_tree(data: bytes, ctx: Ctx, keep: dict[int, str] | None = None) -> str:
    tree = MapTree.parse(data)
    return render_map_tree(entries_from_bin(tree, ctx), struct_to_model(tree.start, "Start", ctx), keep)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# File -> models
# --------------------------------------------------------------------------


def _eval_entry(node: ast.expr) -> M.MapInfo:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        raise CompileError("map tree entries are Map(...) or Area(...)", node)
    kind = node.func.id
    if kind in ENTRY_TYPES:
        for k in node.keywords:
            if k.arg == "type":
                raise CompileError("%s() takes no type: a map is Map(...), an area Area(...)" % kind, k.value)
        typed = ast.Call(
            func=ast.Name(id="MapInfo", ctx=ast.Load()),
            args=node.args,
            keywords=[*node.keywords, ast.keyword(arg="type", value=ast.Constant(ENTRY_TYPES[kind]))],
        )
        ast.copy_location(typed, node)
        ast.copy_location(typed.func, node.func)
        try:
            entry = _eval(typed)
        except CompileError as e:
            raise CompileError(str(e).replace("MapInfo()", "%s()" % kind), node) from None
    elif kind == "MapInfo":
        entry = _eval(node)
    else:
        raise CompileError("map tree entries are Map(...) or Area(...), not %s(...)" % kind, node)
    assert isinstance(entry, M.MapInfo)
    return entry


def _body(tree: ast.Module) -> list[ast.stmt]:
    classes = [st for st in tree.body if isinstance(st, ast.ClassDef)]
    if len(classes) != 1:
        raise CompileError(
            "map_tree.py holds one class, `class %s(%s):`" % (CLASS, BASE), classes[1] if classes else None
        )
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.ClassDef)):
            continue
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == classes[0].name
        ):
            continue  # maps = Maps()
        raise CompileError("top level may only contain `class %s(...)` and `maps = %s()`" % (CLASS, CLASS), stmt)
    return classes[0].body


def compile_map_tree(text: str, ctx: Ctx, filename: str = "<map_tree>") -> list:
    """Parse map_tree.py: MapInfo entries (in file order, with their attribute name in
    ``_ident``; the parent of an entry is the ``with`` block it is in), the TreeOrder of
    the entries that have an id, and the StartSpec."""
    try:
        tree = ast.parse(text, filename)
    except SyntaxError as e:
        raise CompileError("syntax error: %s" % e.msg, e) from None
    entries: list[M.MapInfo] = []
    start: StartSpec | None = None
    names: dict[str, int] = {}  # attribute -> its line
    by_ident: dict[str, M.MapInfo] = {}
    ids: dict[int, int] = {}  # map id -> its line
    blocks: dict[str, int] = {}  # attribute -> the line of its `with` block
    pending: dict[str, str] = {}  # maps under a new map: attribute -> the new map's
    given: list[M.MapInfo] = []  # entries of the top level with a parent_map=

    def walk(body: list[ast.stmt], parent: M.MapInfo | None) -> None:
        nonlocal start
        previous: M.MapInfo | None = None  # the entry right before: a `with` block holds its maps
        for stmt in body:
            if isinstance(stmt, ast.Pass) or (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            if isinstance(stmt, ast.With):
                item = stmt.items[0]
                if (
                    len(stmt.items) != 1
                    or item.optional_vars is not None
                    or not isinstance(item.context_expr, ast.Name)
                ):
                    raise CompileError(
                        "`with name:` holds the maps under the map `name`, defined right before it", stmt
                    )
                ident = item.context_expr.id
                owner = by_ident.get(ident)
                if owner is None:
                    raise CompileError("with %s: %s is not a map of the tree" % (ident, ident), item.context_expr)
                if ident in blocks:
                    raise CompileError(
                        "%s already has its `with` block, on line %d: the maps under it go there"
                        % (ident, blocks[ident]),
                        stmt,
                    )
                if owner is not previous:
                    raise CompileError(
                        "`with %s:` goes right after `%s = Map(...)` (line %d)" % (ident, ident, names[ident]), stmt
                    )
                if owner.type == "area":
                    raise CompileError("%s is an area: no map goes under an area" % ident, item.context_expr)
                blocks[ident] = stmt.lineno
                walk(stmt.body, owner)
                previous = None
                continue
            target = _target(stmt)
            if target is None:
                raise CompileError(
                    "the map tree class may only contain `start = Start(...)`, `name = Map(...)` and `with name:`",
                    stmt,
                )
            value: ast.expr = stmt.value  # type: ignore[union-attr,assignment]
            if target.id in names:
                raise CompileError("%s is defined twice (also on line %d)" % (target.id, names[target.id]), stmt)
            names[target.id] = stmt.lineno
            previous = None
            if target.id == "start":
                if parent is not None:
                    raise CompileError("start goes on the top level of the class, not in a `with` block", stmt)
                model = _eval(value)
                if not isinstance(model, M.Start):
                    raise CompileError("start = Start(...): where a new game begins", value)
                _check_encodable(model, "Start", ctx, value)
                start = StartSpec(start=model, lineno=stmt.lineno)
                continue
            entry = _eval_entry(value)
            if entry.id is not None and (isinstance(entry.id, bool) or entry.id < 1):
                raise CompileError("%s: id must be a positive integer (0 is the root of the tree)" % target.id, value)
            if entry.id is not None and entry.id in ids:
                raise CompileError("map id %d is used twice (also on line %d)" % (entry.id, ids[entry.id]), value)
            if entry.id is not None:
                ids[entry.id] = stmt.lineno
            if len(entry.area_rect) != 4:
                raise CompileError("area_rect is [left, top, right, bottom]", value)
            if entry.type == "area" and not (
                entry.area_rect[0] <= entry.area_rect[2] and entry.area_rect[1] <= entry.area_rect[3]
            ):
                raise CompileError("area_rect is [left, top, right, bottom]: right < left or bottom < top", value)
            explicit = any(k.arg == "parent_map" for k in value.keywords)  # type: ignore[attr-defined]
            if parent is None:
                if explicit:
                    given.append(entry)
            elif explicit and entry.parent_map != parent.id:
                raise CompileError(
                    "%s is in `with %s:`, which is its parent: remove parent_map=" % (target.id, parent._ident), value
                )
            elif parent.id is None:  # a new map: its id is not known yet
                pending[target.id] = parent._ident  # type: ignore[assignment]
            else:
                entry.parent_map = parent.id
            _normalize(entry, "MapInfo")
            _check_encodable(entry, "MapInfo", ctx, value)
            entry._lineno = value.lineno
            entry._ident = target.id
            entries.append(entry)
            by_ident[target.id] = entry
            previous = entry

    walk(_body(tree), None)
    if start is None:
        start = StartSpec(start=M.Start())
    listed = {e.id for e in entries if e.id is not None}
    flat = any(e.parent_map in listed for e in given)
    return [*entries, order_of(entries, pending, flat), start]


def order_of(entries: list[M.MapInfo], pending: dict[str, str] | None = None, flat: bool = False) -> TreeOrder:
    """The tree order of entries listed in this order (those with an id)."""
    parents = {e.id: e.parent_map for e in entries if e.id is not None}
    rank = {mid: n for n, mid in enumerate(parents)}
    return TreeOrder(order=tree_order(parents, rank, {0}), pending=pending or {}, flat=flat)


def resolve_parents(entries: list[M.MapInfo], pending: dict[str, str]) -> None:
    """The maps under new maps take the ids the new maps were just given as parent."""
    ids = {e._ident: e.id for e in entries}
    for e in entries:
        if e._ident in pending:
            e.parent_map = ids[pending[e._ident]]  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Models -> game
# --------------------------------------------------------------------------


def new_info(engine: str, top_level: bool) -> Struct:
    """A new map's MapInfo as the editor makes one: on the top level (nothing to
    inherit from) music from events, battle backgrounds from the terrain, teleport,
    escape and save allowed."""
    st = new_struct("MapInfo", engine)
    if top_level:
        for field in ("music_type", "background_type", "teleport", "escape", "save"):
            st.set(field, 1)
    return st


def blank_map() -> bytes:
    """MapNNNN.lmu of a new map as the editor writes one: 20x15 tiles of the
    first chipset, lower layer water, upper layer transparent, no events."""
    st = Struct("Map")
    st.set(0x0B, b"\x00")  # scroll type: none
    st.set(0x2A, b"\x01")  # top level
    st.set(0x32, b"\x02")  # dungeon generator: the settings RPG Maker writes
    st.set(0x3C, bytes(4 * 9))
    st.set(0x3D, bytes(4 * 9))
    st.set(0x3E, IntVector.encode("i16vec", [49] * 6 + [10000, 10001, 10006, 10007] * 3))
    st.set(0x47, IntVector.encode("i16vec", [0] * 300))
    st.set(0x48, IntVector.encode("i16vec", [10000] * 300))
    st.set("events", [])
    st.set("save_count", 1)
    return LcfFile(header=b"LcfMapUnit", root=st).to_bytes()


class TreeError(ValueError):
    """A map tree the editor can't have; lineno: the entry's line in map_tree.py."""

    def __init__(self, message: str, lineno: int | None = None):
        super().__init__(message)
        self.lineno = lineno


class TreeChanges(BaseModel):
    summary: dict[str, list[int]]
    created: list[int] = []  # new maps: their files are created
    deleted: list[int] = []  # removed maps: their files are deleted


def apply_map_tree(tree: MapTree, specs: list, ctx: Ctx, engine: str) -> TreeChanges:
    """Patch the map tree to hold exactly these entries, in this order, with this start."""
    entries = [s for s in specs if isinstance(s, M.MapInfo)]
    order = next((s for s in specs if isinstance(s, TreeOrder)), None)
    start = next((s for s in specs if isinstance(s, StartSpec)), None)
    summary: dict[str, list[int]] = {"added": [], "removed": [], "changed": []}
    kept = [it for it in tree.maps if is_kept(it)]  # the root (and odd entries): left as they are
    kept_ids = {it.id for it in kept} | {0}
    root_ids = {it.id for it in kept if is_root(it)} or {0}
    current = {it.id: it for it in tree.maps if not is_kept(it)}

    by_id: dict[int, M.MapInfo] = {}
    for e in entries:
        if not isinstance(e.id, int) or isinstance(e.id, bool) or e.id < 1:
            raise TreeError("map id must be a positive integer, got %r" % (e.id,), e.lineno)
        if e.id in by_id:
            raise TreeError("map id %d is used twice" % e.id, e.lineno)
        if e.id > 9999:
            raise TreeError("map %d: map ids go up to 9999 (Map9999.lmu)" % e.id, e.lineno)
        by_id[e.id] = e

    def label(mid: int) -> str:
        e = by_id.get(mid)
        return "%s (map %d)" % (e.name, mid) if e is not None and e.name else "map %d" % mid

    for e in entries:
        if e.type not in ("map", "area"):
            raise TreeError("%s: type must be 'map' or 'area'" % label(e.id), e.lineno)  # type: ignore[arg-type]
        parent = by_id.get(e.parent_map)
        if parent is None and e.parent_map not in kept_ids:
            raise TreeError("%s: parent_map %d is not in the map tree" % (label(e.id), e.parent_map), e.lineno)  # type: ignore[arg-type]
        if parent is not None and parent.type == "area":
            raise TreeError(
                "%s: its parent %s is an area, which has no children" % (label(e.id), label(parent.id)), e.lineno
            )  # type: ignore[arg-type]
        if e.type == "area" and parent is None:
            raise TreeError("%s: an area is part of a map: give it a parent_map" % label(e.id), e.lineno)  # type: ignore[arg-type]
        old = current.get(e.id)  # type: ignore[arg-type]
        if old is not None and map_type(old) != {"map": 1, "area": 2}[e.type]:  # type: ignore[index]
            raise TreeError(
                "%s: a map cannot become an area, nor an area a map: remove it and add a new one" % label(e.id),  # type: ignore[arg-type]
                e.lineno,
            )
    parents = {mid: e.parent_map for mid, e in by_id.items()}
    for mid in parents:
        seen, p = {mid}, parents[mid]
        while p in parents:
            if p in seen:
                raise TreeError("%s is its own ancestor (parent_map loop)" % label(mid), by_id[mid].lineno)
            seen.add(p)
            p = parents[p]
    if start is not None:
        for who, mid in (
            ("party", start.start.party_map_id),
            ("boat", start.start.boat_map_id),
            ("ship", start.start.ship_map_id),
            ("airship", start.start.airship_map_id),
        ):
            if mid and (mid not in by_id or by_id[mid].type != "map"):
                raise TreeError("start: %s_map_id %d is not a map of the tree" % (who, mid), start.lineno)
    removed = sorted(mid for mid in current if mid not in by_id)
    if current and removed and not any(e.type == "map" for e in entries):
        raise TreeError("refusing to delete every map of the game (is map_tree.py complete?)")

    created: list[int] = []
    deleted: list[int] = []
    items: list[ArrayItem] = list(kept)
    for mid in sorted(by_id):
        e = by_id[mid]
        it = current.get(mid)
        if it is None:
            st = new_info(engine, top_level=e.parent_map in root_ids)
            patch_struct(st, "MapInfo", e, ctx, engine, keep_new_defaults=True)
            it = ArrayItem(id=mid, struct=st)
            summary["added"].append(mid)
            if e.type == "map":
                created.append(mid)
        elif patch_struct(it.struct, "MapInfo", e, ctx, engine):
            summary["changed"].append(mid)
        items.append(it)
    for mid in removed:
        summary["removed"].append(mid)
        if map_type(current[mid]) != 2:
            deleted.append(mid)
    # the editor's tree draws each entry at its depth
    for it in items:
        if is_kept(it):
            continue
        depth = _depth(it.id, parents)
        c = it.struct._chunk(INDENTATION)
        if c is None or c.raw is None or not c.raw or Reader(c.raw).ber() != depth:
            it.struct.set(INDENTATION, ber(depth))
    rank: dict[int, float] = {mid: n for n, mid in enumerate(order.order)} if order is not None else {}
    # the kept entries stay in the tree where they are: right after the entry before them
    previous: float = -0.5
    for mid in tree.tree_order:
        if mid in rank:
            previous = rank[mid]
        elif mid in kept_ids and mid not in root_ids:
            rank[mid] = previous = previous + 0.001
            parents[mid] = next(it.struct.get("parent_map") for it in kept if it.id == mid)
    new_order = sorted(root_ids & {it.id for it in kept}) + tree_order(parents, rank, root_ids)  # type: ignore[arg-type]
    if summary["added"] or summary["removed"] or summary["changed"] or len(items) != len(tree.maps):
        tree.maps = sorted(items, key=lambda it: it.id)  # like RPG Maker: by id
    old_order = [m for m in tree.tree_order]
    if new_order != old_order:
        moved = [m for m in new_order if m in current and m in by_id]
        if moved != [m for m in old_order if m in current and m in by_id]:
            summary["order"] = [1]
        tree.tree_order = new_order
    if tree.active_node not in by_id and tree.active_node not in kept_ids:
        tree.active_node = 0
    if start is not None and patch_struct(tree.start, "Start", start.start, ctx, engine):
        summary["start"] = [1]
    return TreeChanges(summary=summary, created=created, deleted=deleted)


# --------------------------------------------------------------------------
# Map references (rpgsync check)
# --------------------------------------------------------------------------


def map_tree_maps(project) -> tuple[dict[int, str] | None, dict[int, tuple[int, int] | None]]:
    """The maps of a project's map tree (id -> name) and their (width, height);
    (None, {}) when it has no readable map tree."""
    try:
        tree = MapTree.load(project.lmt_path)
    except (OSError, LcfError):
        return None, {}
    names = project.map_names()
    maps = {it.id: names.get(it.id, "") for it in tree.maps if map_type(it) == 1}
    return maps, map_sizes(project.game_dir, maps)


def map_sizes(game_dir: str, ids) -> dict[int, tuple[int, int] | None]:
    """map id -> (width, height) of its MapNNNN.lmu, None when it cannot be read."""
    out: dict[int, tuple[int, int] | None] = {}
    for mid in ids:
        path = os.path.join(game_dir, "Map%04d.lmu" % mid)
        try:
            root = LcfFile.load(path).root
            out[mid] = (root.get("width"), root.get("height"))
        except (OSError, LcfError):
            out[mid] = None
    return out


# command code -> (index of the map id, of x, of y, of a mode that must be 0 for a fixed place)
MAP_PARAMS = {
    10810: (0, 1, 2, None),  # teleport
    10850: (2, 3, 4, 1),  # set_vehicle_location
    11810: (1, 2, 3, None),  # add/remove_teleport_target (index 0: 0 = add)
    11830: (0, 1, 2, None),  # set_escape_target
}
COMMAND_NAMES = {
    10810: "teleport",
    10850: "set_vehicle_location",
    11810: "teleport target",
    11830: "set_escape_target",
}


def _commands(spec):
    """Every command of an event / common event / troop spec."""
    pages = getattr(spec, "pages", None)
    lists = [p.commands for p in pages] if pages is not None else [getattr(spec, "commands", None) or []]
    for commands in lists:
        yield from commands


def map_reference_problems(
    specs: list, maps: dict[int, str], sizes: dict[int, tuple[int, int] | None]
) -> list[tuple[int | None, str]]:
    """Commands that send the party (or a vehicle, a teleport / escape target) to a map
    that is not in the game, or outside the map.  -> (script line, problem)
    maps: id -> name of the maps of the tree; sizes: their (width, height)."""
    out: list[tuple[int | None, str]] = []
    for spec in specs:
        if isinstance(spec, StartSpec):  # where the party and the vehicles start a new game
            for who in ("party", "boat", "ship", "airship"):
                mid, x, y = (getattr(spec.start, "%s_%s" % (who, f)) for f in ("map_id", "x", "y"))
                problem = _place_problem("the %s starts on" % who, mid, x, y, maps, sizes) if mid else None
                if problem:
                    out.append((spec.lineno, problem))
            continue
        for c in _commands(spec):
            where = MAP_PARAMS.get(getattr(c, "code", None))  # type: ignore[arg-type]
            if where is None:
                continue
            mi, xi, yi, mode = where
            p = list(c.params)
            if len(p) <= yi or (mode is not None and p[mode] != 0):
                continue  # too short, or a place given by variables
            if c.code == 11810 and p[0] != 0:
                continue  # remove_teleport_target: any map
            mid, x, y = p[mi], p[xi], p[yi]
            if c.code == 10850 and mid == 0:
                continue  # vehicle: 0 = not placed
            problem = _place_problem(COMMAND_NAMES[c.code] + " to", mid, x, y, maps, sizes)
            if problem:
                out.append((c.line, problem))
    return out


def _place_problem(
    what: str, mid: int, x: int, y: int, maps: dict[int, str], sizes: dict[int, tuple[int, int] | None]
) -> str | None:
    """Why (x, y) of map `mid` is not a place of the game, None when it is."""
    if mid not in maps:
        return "%s map %d, which is not in the map tree" % (what, mid)
    size = sizes.get(mid)
    if size is None:
        return "%s %s (map %d): Map%04d.lmu is missing" % (what, maps[mid], mid, mid)
    if not (0 <= x < size[0] and 0 <= y < size[1]):
        return "%s %s (map %d) at (%d, %d): outside the map (%dx%d)" % (what, maps[mid], mid, x, y, size[0], size[1])
    return None


def tree_problems(game_dir: str, script_dir: str, encoding: str) -> tuple[list[str], list[str]]:
    """The map tree against the map files and the scripts: (errors, warnings).
    Errors: start positions on a missing map, maps of the tree without a file.
    Warnings: map files out of the tree, MapNNNN.py scripts whose map is gone."""
    errors: list[str] = []
    warnings: list[str] = []
    lmt = os.path.join(game_dir, TARGET)
    try:
        tree = MapTree.load(lmt)
    except (OSError, LcfError) as e:
        return ["%s cannot be read: %s" % (TARGET, e)], []
    maps = {it.id: it for it in tree.maps if map_type(it) == 1}
    files = {}
    for name in os.listdir(game_dir):
        m = re.fullmatch(r"map(\d{4})\.lmu", name, re.I)
        if m:
            files[int(m.group(1))] = name
    for mid, it in sorted(maps.items()):
        if mid not in files:
            errors.append(
                "map %d (%s) has no file Map%04d.lmu"
                % (mid, bytes(it.struct.get("name")).decode(encoding, "replace"), mid)
            )
    for mid, name in sorted(files.items()):
        if mid not in maps:
            warnings.append("%s is not in the map tree: the editor doesn't list it" % name)
    start = tree.start
    for who in ("party", "boat", "ship", "airship"):
        mid = start.get(who + "_map_id")
        if mid and mid not in maps:
            errors.append("the %s starts on map %d, which is not in the map tree" % (who, mid))
    names = os.listdir(script_dir) if os.path.isdir(script_dir) else []
    for mid, name in sorted(script_map_ids(names, script_dir).items()):
        if mid not in files:
            warnings.append("%s: the game has no map %d any more (deleted in the editor?)" % (name, mid))
    return errors, warnings


def script_map_ids(names: list[str], script_dir: str | None = None) -> dict[int, str]:
    """MapNNNN.py file names (and MapNNNN/ folders of a map split in files, in
    `script_dir`) -> map id."""
    from .scriptfiles import is_package

    out = {}
    for name in names:
        m = re.fullmatch(r"Map(\d{4})(\.py)?", name)
        if m and m.group(2):
            out[int(m.group(1))] = name
        elif m and script_dir and is_package(os.path.join(script_dir, name + ".py")):
            out[int(m.group(1))] = name + "/"
    return out
