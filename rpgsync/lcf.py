"""Lossless reader/writer for RPG Maker 2000/2003 LCF files (.lmu, .ldb, .lmt).

LCF is a chunked binary format.  Every structure is a list of chunks
``(id, length, payload)`` terminated by a ``0`` id, integers are BER
compressed, and arrays are ``count`` followed by ``(id, struct)`` pairs.

The design goal of this module is *byte exact* round trips: a file that is
read and written back without modification yields identical bytes.  To get
there, a :class:`Struct` keeps every chunk in its original order.  Chunks are
decoded lazily according to a small schema; chunks that are never touched are
written back verbatim, and chunks unknown to the schema are kept as raw bytes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LcfError(Exception):
    pass


# --------------------------------------------------------------------------
# Primitive codecs
# --------------------------------------------------------------------------


class Reader:
    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def eof(self) -> bool:
        return self.pos >= self.end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise LcfError("unexpected end of data at 0x%x" % self.pos)
        b = self.data[self.pos]
        self.pos += 1
        return b

    def peek(self) -> int:
        return self.data[self.pos] if self.pos < self.end else -1

    def ber(self) -> int:
        """Read a BER compressed integer.  Values are 32 bit two's complement."""
        value = 0
        while True:
            b = self.byte()
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                break
        value &= 0xFFFFFFFF
        return value - 0x100000000 if value & 0x80000000 else value

    def take(self, n: int) -> bytes:
        if self.pos + n > self.end:
            raise LcfError("chunk of %d bytes at 0x%x overruns data" % (n, self.pos))
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b


def ber(value: int) -> bytes:
    """Encode an integer exactly like RPG_RT / liblcf do."""
    v = value & 0xFFFFFFFF
    out = bytearray()
    for shift in (28, 21, 14, 7):
        if v >= (1 << shift):
            out.append(((v >> shift) & 0x7F) | 0x80)
    out.append(v & 0x7F)
    return bytes(out)


def ber_size(value: int) -> int:
    return len(ber(value))


# --------------------------------------------------------------------------
# Event commands
# --------------------------------------------------------------------------


class Command(BaseModel):
    """One line of an event script."""

    code: int
    indent: int = 0
    string: bytes = b""
    params: list[int] = Field(default_factory=list)

    def key(self) -> tuple:
        return (self.code, self.indent, self.string, tuple(self.params))


def read_commands(data: bytes) -> tuple[list[Command], bytes]:
    """Decode an event command list.

    Returns the command list and any trailing bytes after the 4 byte
    terminator (normally empty; kept so odd files still round trip)."""
    r = Reader(data)
    cmds: list[Command] = []
    while True:
        if r.eof():
            raise LcfError("event command list without terminator")
        if r.peek() == 0:
            term = r.take(4)
            if term != b"\0\0\0\0":
                raise LcfError("bad event command terminator %r" % term)
            break
        code = r.ber()
        indent = r.ber()
        string = r.take(r.ber())
        params = [r.ber() for _ in range(r.ber())]
        # trusted data straight from the parser: skip validation (hot path)
        cmds.append(Command.model_construct(code=code, indent=indent, string=string, params=params))
    return cmds, data[r.pos :]


def write_commands(cmds: list[Command], trailer: bytes = b"") -> bytes:
    out = bytearray()
    for c in cmds:
        out += ber(c.code)
        out += ber(c.indent)
        out += ber(len(c.string))
        out += c.string
        out += ber(len(c.params))
        for p in c.params:
            out += ber(p)
    out += b"\0\0\0\0"
    out += trailer
    return bytes(out)


# --------------------------------------------------------------------------
# Move commands (move routes)
# --------------------------------------------------------------------------

MOVE_SWITCH_ON = 32
MOVE_SWITCH_OFF = 33
MOVE_CHANGE_GRAPHIC = 34
MOVE_PLAY_SE = 35


class MoveCommand(BaseModel):
    """One step of a move route."""

    code: int
    string: bytes = b""
    params: list[int] = Field(default_factory=list)

    def key(self):
        return (self.code, self.string, tuple(self.params))


def read_move_commands(data: bytes) -> list[MoveCommand]:
    r = Reader(data)
    out = []
    while not r.eof():
        code = r.ber()
        if code in (MOVE_SWITCH_ON, MOVE_SWITCH_OFF):
            out.append(MoveCommand(code=code, params=[r.ber()]))
        elif code == MOVE_CHANGE_GRAPHIC:
            s = r.take(r.ber())
            out.append(MoveCommand(code=code, string=s, params=[r.ber()]))
        elif code == MOVE_PLAY_SE:
            s = r.take(r.ber())
            out.append(MoveCommand(code=code, string=s, params=[r.ber(), r.ber(), r.ber()]))
        else:
            out.append(MoveCommand(code=code))
    return out


def write_move_commands(cmds: list[MoveCommand]) -> bytes:
    out = bytearray()
    for c in cmds:
        out += ber(c.code)
        if c.code in (MOVE_CHANGE_GRAPHIC, MOVE_PLAY_SE):
            out += ber(len(c.string)) + c.string
        for p in c.params:
            out += ber(p)
    return bytes(out)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
#
# Field types:
#   "int"        BER integer
#   "bool"       BER integer, exposed as bool
#   "str"        raw bytes (string in game encoding)
#   "commands"   event command list
#   "moves"      move command list
#   "S:<Name>"   nested struct
#   "A:<Name>"   array of structs (count, then id + struct each)
#   "i16vec"     little endian int16 array (Vector<Int16>, Parameters, Equipment)
#   "i32vec"     little endian int32 array (Vector<Int32>)
#   "u8vec"      byte array (Vector<UInt8>, DBBitArray, bit flags)
#   "raw"        bytes, untouched (also used for anything not listed)
#
# The database structures (Actor, Skill, ...) are added at the end of this
# module from the generated liblcf schema in dbschema.py.

SCHEMA: dict[str, dict[int, tuple[str, str]]] = {
    "Map": {
        0x01: ("chipset_id", "int"),
        0x02: ("width", "int"),
        0x03: ("height", "int"),
        0x51: ("events", "A:Event"),
        0x5A: ("save_count_2k3e", "int"),
        0x5B: ("save_count", "int"),
    },
    "Event": {
        0x01: ("name", "str"),
        0x02: ("x", "int"),
        0x03: ("y", "int"),
        0x05: ("pages", "A:EventPage"),
    },
    "EventPage": {
        0x02: ("condition", "S:EventPageCondition"),
        0x15: ("character_name", "str"),
        0x16: ("character_index", "int"),
        0x17: ("character_direction", "int"),
        0x18: ("character_pattern", "int"),
        0x19: ("translucent", "bool"),
        0x1F: ("move_type", "int"),
        0x20: ("move_frequency", "int"),
        0x21: ("trigger", "int"),
        0x22: ("layer", "int"),
        0x23: ("overlap_forbidden", "bool"),
        0x24: ("animation_type", "int"),
        0x25: ("move_speed", "int"),
        0x29: ("move_route", "S:MoveRoute"),
        0x33: ("event_commands_size", "int"),
        0x34: ("event_commands", "commands"),
    },
    "EventPageCondition": {
        0x01: ("flags", "int"),
        0x02: ("switch_a_id", "int"),
        0x03: ("switch_b_id", "int"),
        0x04: ("variable_id", "int"),
        0x05: ("variable_value", "int"),
        0x06: ("item_id", "int"),
        0x07: ("actor_id", "int"),
        0x08: ("timer_sec", "int"),
        0x09: ("timer2_sec", "int"),
        0x0A: ("compare_operator", "int"),
    },
    "MoveRoute": {
        0x0B: ("move_commands_size", "int"),
        0x0C: ("move_commands", "moves"),
        0x15: ("repeat", "bool"),
        0x16: ("skippable", "bool"),
    },
    "Database": {
        0x0B: ("actors", "A:Named"),
        0x0C: ("skills", "A:Named"),
        0x0D: ("items", "A:Named"),
        0x0E: ("enemies", "A:Named"),
        0x0F: ("troops", "A:Named"),
        0x17: ("switches", "A:Named"),
        0x18: ("variables", "A:Named"),
        0x19: ("commonevents", "A:CommonEvent"),
    },
    # Any database entry whose name lives in chunk 0x01; the rest is kept raw.
    "Named": {
        0x01: ("name", "str"),
    },
    "CommonEvent": {
        0x01: ("name", "str"),
        0x0B: ("trigger", "int"),
        0x0C: ("switch_flag", "bool"),
        0x0D: ("switch_id", "int"),
        0x15: ("event_commands_size", "int"),
        0x16: ("event_commands", "commands"),
    },
    "TreeMap": {},
    "MapInfo": {
        0x01: ("name", "str"),
        0x02: ("parent_map", "int"),
        0x04: ("type", "int"),
    },
}

# Default values (liblcf generator/csv/fields.csv), used when a chunk is absent.
DEFAULTS: dict[str, dict[str, Any]] = {
    "Event": {"name": b"", "x": 0, "y": 0},
    "EventPage": {
        "character_name": b"",
        "character_index": 0,
        "character_direction": 2,
        "character_pattern": 1,
        "translucent": False,
        "move_type": 1,
        "move_frequency": 3,
        "trigger": 0,
        "layer": 0,
        "overlap_forbidden": False,
        "animation_type": 0,
        "move_speed": 3,
    },
    "EventPageCondition": {
        "flags": 0,
        "switch_a_id": 1,
        "switch_b_id": 1,
        "variable_id": 1,
        "variable_value": 0,
        "item_id": 1,
        "actor_id": 1,
        "timer_sec": 0,
        "timer2_sec": 0,
        "compare_operator": 1,
    },
    "MoveRoute": {"repeat": True, "skippable": False},
    "CommonEvent": {"name": b"", "trigger": 5, "switch_flag": False, "switch_id": 1},
    "Named": {"name": b""},
    "Map": {"chipset_id": 1, "width": 20, "height": 15, "save_count": 0, "save_count_2k3e": 0},
    "MapInfo": {"name": b"", "parent_map": 0, "type": -1},
}

# Fields that store the byte size of the following field.
SIZE_FIELDS = {
    ("EventPage", 0x33): 0x34,
    ("CommonEvent", 0x15): 0x16,
    ("MoveRoute", 0x0B): 0x0C,
}

OPTIONAL_SIZE_FIELDS = {("MoveRoute", 0x0B)}

# Order in which RPG Maker writes the chunks of a freshly created structure.
CHUNK_ORDER = {name: sorted(fields) for name, fields in SCHEMA.items()}


class Chunk:
    """A chunk keeps its original bytes until its value is decoded and changed."""

    __slots__ = ("id", "raw", "_value", "_decoded", "dirty")

    def __init__(self, cid: int, raw: bytes | None = None):
        self.id = cid
        self.raw = raw
        self._value = None
        self._decoded = False
        self.dirty = raw is None


class Struct:
    """An ordered list of chunks with typed access through SCHEMA."""

    def __init__(self, name: str, chunks: list[Chunk] | None = None, terminated: bool = True):
        self.name = name
        self.chunks: list[Chunk] = chunks if chunks is not None else []
        self.terminated = terminated

    # -- schema helpers --------------------------------------------------
    def _field(self, key) -> tuple[int, str, str]:
        fields = SCHEMA.get(self.name, {})
        if isinstance(key, int):
            fname, ftype = fields.get(key, ("chunk_%02x" % key, "raw"))
            return key, fname, ftype
        for cid, (fname, ftype) in fields.items():
            if fname == key:
                return cid, fname, ftype
        raise KeyError("%s has no field %r" % (self.name, key))

    def _chunk(self, cid: int) -> Chunk | None:
        for c in self.chunks:
            if c.id == cid:
                return c
        return None

    def has(self, key) -> bool:
        return self._chunk(self._field(key)[0]) is not None

    def get(self, key, default: Any = "__schema__"):
        cid, fname, ftype = self._field(key)
        c = self._chunk(cid)
        if c is None:
            if default == "__schema__":
                d = DEFAULTS.get(self.name, {}).get(fname)
                if ftype.startswith("A:") or ftype in ("commands", "moves"):
                    return [] if d is None else d
                if ftype.startswith("S:"):
                    return Struct(ftype[2:])
                return d
            return default
        if not c._decoded:
            c._value = decode_value(ftype, c.raw)
            c._decoded = True
        return c._value

    def set(self, key, value) -> None:
        cid, fname, ftype = self._field(key)
        c = self._chunk(cid)
        if c is None:
            c = Chunk(cid)
            # insert keeping ascending chunk id order (the order RPG Maker uses)
            idx = len(self.chunks)
            for i, other in enumerate(self.chunks):
                if other.id > cid:
                    idx = i
                    break
            self.chunks.insert(idx, c)
        c._value = value
        c._decoded = True
        c.dirty = True
        # size fields precede their data chunk; like the RPG Maker editor (and
        # liblcf), a move route's size is only written for non-empty routes
        for (sname, size_id), data_id in SIZE_FIELDS.items():
            if sname == self.name and data_id == cid:
                has_size = self._chunk(size_id) is not None
                if (sname, size_id) in OPTIONAL_SIZE_FIELDS and not value:
                    if has_size:
                        self.remove(size_id)
                elif not has_size:
                    self.set(size_id, 0)

    def remove(self, key) -> None:
        cid = self._field(key)[0]
        self.chunks = [c for c in self.chunks if c.id != cid]

    def mark_dirty(self, key) -> None:
        """Call after mutating a decoded list/struct in place."""
        c = self._chunk(self._field(key)[0])
        if c is not None:
            c.dirty = True

    def __repr__(self):
        return "<Struct %s %s>" % (self.name, [hex(c.id) for c in self.chunks])


class ArrayItem(BaseModel):
    """One element of an LCF array: the element id and its struct."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int
    struct: Struct


# --------------------------------------------------------------------------
# Decoding / encoding values
# --------------------------------------------------------------------------


def decode_value(ftype: str, raw: bytes):
    if ftype == "int":
        return Reader(raw).ber() if raw else 0
    if ftype == "bool":
        return (Reader(raw).ber() if raw else 0) > 0
    if ftype == "str" or ftype == "raw":
        return raw
    if ftype == "commands":
        cmds, trailer = read_commands(raw)
        return CommandList(cmds, trailer)
    if ftype == "moves":
        return read_move_commands(raw)
    if ftype in VECTOR_FORMATS:
        return IntVector.decode(ftype, raw)
    if ftype.startswith("S:"):
        r = Reader(raw)
        s = read_struct(r, ftype[2:])
        if not r.eof():
            raise LcfError("trailing bytes after struct %s" % ftype)
        return s
    if ftype.startswith("A:"):
        r = Reader(raw)
        arr = read_array(r, ftype[2:])
        if not r.eof():
            raise LcfError("trailing bytes after array %s" % ftype)
        return arr
    raise LcfError("unknown field type %s" % ftype)


def encode_value(ftype: str, value) -> bytes:
    if ftype == "int":
        return ber(value)
    if ftype == "bool":
        return ber(1 if value else 0)
    if ftype in ("str", "raw"):
        return bytes(value)
    if ftype == "commands":
        if isinstance(value, CommandList):
            return write_commands(value, value.trailer)
        return write_commands(value)
    if ftype == "moves":
        return write_move_commands(value)
    if ftype in VECTOR_FORMATS:
        return IntVector.encode(ftype, value)
    if ftype.startswith("S:"):
        return write_struct(value)
    if ftype.startswith("A:"):
        return write_array(value)
    raise LcfError("unknown field type %s" % ftype)


VECTOR_FORMATS = {"i16vec": ("h", 2), "i32vec": ("i", 4), "u8vec": ("B", 1)}


class IntVector(list):
    """Fixed-width integer array; remembers bytes that do not fill a whole
    element (malformed data) so that it still round trips."""

    def __init__(self, values=(), trailer: bytes = b""):
        super().__init__(values)
        self.trailer = trailer

    @classmethod
    def decode(cls, ftype: str, raw: bytes) -> IntVector:
        import struct as _struct

        code, size = VECTOR_FORMATS[ftype]
        n = len(raw) // size
        return cls(_struct.unpack("<%d%s" % (n, code), raw[: n * size]), raw[n * size :])

    @staticmethod
    def encode(ftype: str, values) -> bytes:
        import struct as _struct

        code, size = VECTOR_FORMATS[ftype]
        try:
            data = _struct.pack("<%d%s" % (len(values), code), *values)
        except _struct.error as e:
            raise LcfError("value out of range for %s: %s" % (ftype, e)) from None
        return data + getattr(values, "trailer", b"")


class CommandList(list):
    """List of Command that remembers bytes trailing the terminator."""

    def __init__(self, cmds=(), trailer: bytes = b""):
        super().__init__(cmds)
        self.trailer = trailer


def read_struct(r: Reader, name: str) -> Struct:
    s = Struct(name, terminated=False)
    while not r.eof():
        cid = r.ber()
        if cid == 0:
            s.terminated = True
            break
        size = r.ber()
        s.chunks.append(Chunk(cid, r.take(size)))
    return s


def read_array(r: Reader, name: str) -> list[ArrayItem]:
    count = r.ber()
    items = []
    for _ in range(count):
        iid = r.ber()
        items.append(ArrayItem(id=iid, struct=read_struct(r, name)))
    return items


def _chunk_bytes(s: Struct, c: Chunk) -> bytes:
    """Bytes of a chunk payload, re-encoding only when something changed."""
    if c._decoded and (c.dirty or _is_container_dirty(c._value)):
        _, _, ftype = s._field(c.id)
        return encode_value(ftype, c._value)
    return c.raw


def _is_container_dirty(value) -> bool:
    if isinstance(value, Struct):
        return any(ch.dirty or (ch._decoded and _is_container_dirty(ch._value)) for ch in value.chunks)
    if isinstance(value, list) and value and isinstance(value[0], ArrayItem):
        return any(_is_container_dirty(it.struct) for it in value)
    return False


def write_struct(s: Struct, terminate: bool | None = None) -> bytes:
    payloads = [(c, _chunk_bytes(s, c)) for c in s.chunks]
    # keep size chunks consistent with their data chunk
    sizes = {}
    for (sname, size_id), data_id in SIZE_FIELDS.items():
        if sname == s.name:
            for c, p in payloads:
                if c.id == data_id:
                    sizes[size_id] = _logical_size(s, c, p)
    out = bytearray()
    for c, p in payloads:
        if c.id in sizes:
            current = decode_value("int", p)
            if current != sizes[c.id]:
                p = ber(sizes[c.id])
                c._value, c._decoded, c.raw = sizes[c.id], True, p
        out += ber(c.id)
        out += ber(len(p))
        out += p
    if s.terminated if terminate is None else terminate:
        out += b"\0"
    return bytes(out)


def _logical_size(s: Struct, c: Chunk, payload: bytes) -> int:
    """The 'size' chunk preceding event commands stores a byte count.

    RPG_RT stores the byte length of the command data (including the 4 byte
    terminator) for event commands and move commands."""
    return len(payload)


def write_array(items: list[ArrayItem]) -> bytes:
    out = bytearray(ber(len(items)))
    for it in items:
        out += ber(it.id)
        out += write_struct(it.struct)
    return bytes(out)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


class LcfFile(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    header: bytes  # e.g. b"LcfMapUnit"
    root: Struct
    trailer: bytes = b""  # bytes after the root struct (should be empty)

    @classmethod
    def load(cls, path: str, root_name: str | None = None) -> LcfFile:
        with open(path, "rb") as f:
            return cls.parse(f.read(), root_name)

    @classmethod
    def parse(cls, data: bytes, root_name: str | None = None) -> LcfFile:
        r = Reader(data)
        header = r.take(r.ber())
        if root_name is None:
            root_name = {b"LcfMapUnit": "Map", b"LcfDataBase": "Database", b"LcfMapTree": "TreeMap"}.get(header)
            if root_name is None:
                raise LcfError("unsupported LCF file type %r" % header)
        if root_name == "TreeMap":
            # The map tree is not chunked at top level; we only need map names.
            return cls(header=header, root=Struct("TreeMap", [Chunk(-1, data[r.pos :])], False))
        root = read_struct(r, root_name)
        return cls(header=header, root=root, trailer=data[r.pos :])

    def to_bytes(self) -> bytes:
        return ber(len(self.header)) + self.header + write_struct(self.root) + self.trailer

    def save(self, path: str) -> None:
        import os
        import tempfile

        data = self.to_bytes()
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix=".rpgsync-", dir=d)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def read_map_tree_names(path: str) -> dict[int, bytes]:
    """Map id -> raw name from RPG_RT.lmt (only the MapInfo array is read)."""
    with open(path, "rb") as f:
        data = f.read()
    r = Reader(data)
    r.take(r.ber())
    names = {}
    for item in read_array(r, "MapInfo"):
        names[item.id] = item.struct.get("name")
    return names


# --------------------------------------------------------------------------
# Database structures (generated liblcf schema)
# --------------------------------------------------------------------------


def _register_db_schema() -> None:
    """Add the database tables' structures to SCHEMA / DEFAULTS / SIZE_FIELDS.

    Structures already described above (CommonEvent, ...) are left alone;
    unexported tables (animations, terms, system, ...) stay raw."""
    from . import dbschema

    for sname in dbschema.MODEL_STRUCTS:
        if sname in SCHEMA:
            continue
        fields: dict[int, tuple[str, str]] = {}
        defaults: dict[str, Any] = {}
        for f in dbschema.STRUCTS[sname]:
            if f.id is None:  # packed structure (Parameters, Equipment)
                continue
            if f.size_of is not None:
                fields[f.id] = (f.name + "_size", "int")
                SIZE_FIELDS[(sname, f.id)] = f.size_of
                if not f.persist:
                    OPTIONAL_SIZE_FIELDS.add((sname, f.id))
                continue
            fields[f.id] = (f.name, f.kind)
            if f.kind in ("int", "bool") and f.default == f.default_2k3:
                defaults[f.name] = f.default
            elif f.kind == "str":
                defaults[f.name] = f.default.encode("ascii")
            elif f.kind in VECTOR_FORMATS:
                defaults[f.name] = IntVector()
        if fields:
            SCHEMA[sname] = fields
            DEFAULTS[sname] = defaults
            CHUNK_ORDER[sname] = sorted(fields)
    for table, (cid, sname, _title) in dbschema.TABLES.items():
        if sname in SCHEMA:
            SCHEMA["Database"][cid] = (table, "A:" + sname)
    CHUNK_ORDER["Database"] = sorted(SCHEMA["Database"])


_register_db_schema()
