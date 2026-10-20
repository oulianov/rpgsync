"""A script written as one file, or as a folder of files when it is too big for one.

GitLab shows no diff of a file whose patch is over 200 KB (and collapses the ones over
20 KB): a script bigger than ``max_file_kb`` ([tool.rpgsync] of Scripts/pyproject.toml,
160 by default) is written as a Python package instead::

    database/common_events/__init__.py           its header, Config and instance
    database/common_events/events_0001_0036.py   class Events_0001_0036(CommonEventTable): ...
    database/common_events/events_0037_0097.py   class Events_0037_0097(CommonEventTable): ...

The class of __init__.py inherits from the class of every part, so ty and Pylance find
the entries where they found them before (``common_events.mr_clean``, ``variables.gold``),
and every ``from database.common_events import common_events`` keeps working.  The
events of a map are at the top level of its parts (Map0195/events_0001_0040.py); a part
imports the events of the other parts it names.

rpgsync itself sees one text: the script as it would be in one file (the "joined" text).
A folder is joined when it is read, with a line map back to the files, and split again
when it is written.  Every .py file of the folder is a part, in the order of their names.
What is above the class of a part, and the imports of the parts and the class statement
of __init__.py, are rpgsync's; the rest is yours.  Writing a script into its folder keeps
every entry in its file and in its place: a new entry goes after the one before it (by
id), an emptied part is removed, and a part grown over the limit is cut in two.  The
first split cuts the script by size, in id order, into parts named after their ids.
Parts are never renamed nor merged back; a part holding one entry bigger than the limit
stays as it is.
"""

from __future__ import annotations

import ast
import bisect
import io
import os
import re
import tokenize
from collections.abc import Callable

from pydantic import BaseModel

KB = 1024
MAX_FILE_KB = 160  # default limit (GitLab shows no diff of a file over 200 KB)
FILL = 0.75  # a new part is filled up to this share of the limit: it has room to grow
LINE_LENGTH = 120

MARK = (
    "# Its entries are in the other files of this folder, in the order of their names: rpgsync",
    "# splits a script bigger than %d KB (GitLab shows no diff of a bigger file). Move them freely.",
)
_MARK_2 = re.compile(re.escape(MARK[1]).replace("%d", r"\d+"))
_CLASS = re.compile(r"class (\w+)\((\w+)\):\s*(?:#.*)?$")
_TABLE_ENTRY = re.compile(r"    [A-Za-z_]\w*\s*(?::[^=]*)?=(?!=)")
_POSITIONAL_ID = re.compile(r"\s*\w+\s*(?::[^=]*)?=\s*\w+\(\s*(\d+)\s*[,)]")
_FIRST_KEYWORD_ID = re.compile(r"=\s*\w+\(\s*id=(\d+)\b")  # potion = Item(id=5, ...)
_NEXT_LINE_ID = re.compile(r"\s+id=(\d+),")  # its first argument, on the next line
_TOP_DEF = re.compile(r"(?:class|def) (\w+)\b")


class ChangedMeanwhile(Exception):
    """A file changed while rpgsync was syncing it (an edit in the editor or in the
    script): nothing more is written, the next pass syncs both changes."""


class LayoutError(Exception):
    """A script folder rpgsync cannot read as one script."""

    def __init__(self, path: str, message: str, line: int | None = None):
        self.path = path
        self.message = message
        self.line = line
        super().__init__("%s%s: %s" % (path, ":%d" % line if line else "", message))


class Style(BaseModel):
    """How the entries of a kind of script are laid out."""

    wrapped: bool  # in a class (common events, troops, tables), else at the top level (maps)
    decorator: str | None = None  # entries are `@decorator(id, ...)`; None: `name = Model(id=...)`
    prefix: str  # names of the part files: <prefix>_<first id>_<last id>.py
    sep: int  # blank lines between two entries
    self_import: str | None = None  # what a part imports to name its own script's instance
    event_refs: bool = False  # maps: a part imports the event classes it names from the others

    @property
    def indent(self) -> str:
        return "    " if self.wrapped else ""


COMMON_EVENTS = Style(
    wrapped=True,
    decorator="common_event",
    prefix="events",
    sep=1,
    self_import="from database.common_events import common_events",
)
TROOP_EVENTS = Style(wrapped=True, decorator="troop", prefix="troops", sep=1)
MAP = Style(wrapped=False, decorator="event", prefix="events", sep=2, event_refs=True)


def table_style(table: str) -> Style:
    """A database table: `name = Model(id=...)` entries, no blank line between them."""
    return Style(wrapped=True, prefix=table, sep=0)


class Part(BaseModel):
    """A part of a script folder, as read."""

    path: str
    cls: str | None  # its class (None: a map's part)
    first: int  # the lines of its entries in the joined text: [first, end) (0-based)
    end: int


class ScriptFiles(BaseModel):
    """A script as read: its joined text, the bytes of each of its files (to tell
    whether they changed since), and where each line of the text comes from."""

    path: str  # the script's path as one file (database/common_events.py)
    text: str
    raw: dict[str, bytes]
    package: bool = False
    parts: list[Part] = []
    spans: list[tuple[int, str, int, int]] = []  # (joined line index, file, its line number, count)

    def where(self, lineno: int | None) -> tuple[str, int | None]:
        """The file and line of a line of the joined text (1-based)."""
        if lineno is None:
            return (package_dir(self.path) if self.package else self.path, None)
        if not self.spans:
            return (self.path, lineno)
        n = bisect.bisect_right(self.spans, (lineno - 1, "￿")) - 1
        start, path, line, count = self.spans[max(n, 0)]
        return (path, line + min(lineno - 1 - start, max(count - 1, 0)))

    def entry_files(self, style: Style | None) -> dict[int, str]:
        """id -> the file of each entry, for a script split in files."""
        lay = layout(self.text, style) if self.package and style is not None else None
        if lay is None:
            return {}
        return {c.id: self.where(c.start + 1)[0] for c in lay.chunks if c.id is not None}

    def at(self, lineno: int | None) -> str:
        """The file and line of a line of the joined text, as path:line."""
        path, line = self.where(lineno)
        return "%s:%d" % (path, line) if line else path


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def max_file_kb(script_dir: str) -> int:
    """[tool.rpgsync] max_file_kb of a scripts folder's pyproject.toml: a script bigger
    than this is split in files (0: never)."""
    from .compat import load_config

    value = load_config(script_dir).get("max_file_kb", MAX_FILE_KB)
    return value if isinstance(value, int) and value >= 0 else MAX_FILE_KB


def package_dir(py_path: str) -> str:
    return py_path[:-3] if py_path.endswith(".py") else py_path


def _package_files(py_path: str) -> list[str]:
    folder = package_dir(py_path)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(os.path.join(folder, n) for n in names if n.endswith(".py") and not n.startswith("."))


def is_package(py_path: str) -> bool:
    return bool(_package_files(py_path))


def exists(py_path: str) -> bool:
    return os.path.exists(py_path) or is_package(py_path)


def paths(py_path: str) -> list[str]:
    """The files of a script now (one file, or those of its folder), its path as one
    file first even when missing: what to watch."""
    return [py_path, *_package_files(py_path)]


def _stat(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def stat_key(py_path: str) -> tuple:
    """Changes when a file of the script changes, appears or goes."""
    return tuple((p, _stat(p)) for p in paths(py_path))


def read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".rpgsync-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _norm(data: bytes) -> str:
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def unchanged(py_path: str, expect: ScriptFiles | None) -> bool:
    """Are the files of the script still the ones read (`expect`, None: no script)?"""
    current = [p for p in paths(py_path) if os.path.exists(p)]
    if expect is None:
        return not current
    return set(current) == set(expect.raw) and all(read_bytes(p) == expect.raw[p] for p in current)


def check_unchanged(py_path: str, expect: ScriptFiles | None) -> None:
    if not unchanged(py_path, expect):
        raise ChangedMeanwhile(os.path.basename(package_dir(py_path) if is_package(py_path) else py_path))


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def read(py_path: str, style: Style | None) -> ScriptFiles | None:
    """The script (None: there is none).  Raises LayoutError for a folder that is not
    one script, ChangedMeanwhile when its files keep changing while they are read."""
    for _ in range(5):
        before = stat_key(py_path)
        found = _read(py_path, style)
        if stat_key(py_path) == before:  # a git checkout, a refactoring over several files...
            return found
    raise ChangedMeanwhile(os.path.basename(py_path))


def read_text(py_path: str, style: Style | None) -> str | None:
    found = read(py_path, style)
    return None if found is None else found.text


def _read(py_path: str, style: Style | None) -> ScriptFiles | None:
    files = _package_files(py_path)
    single = read_bytes(py_path)
    if not files:
        if single is None:
            return None
        return ScriptFiles(path=py_path, text=_norm(single), raw={py_path: single})
    folder = package_dir(py_path)
    if single is not None:
        raise LayoutError(py_path, "the folder %s/ holds this script too: keep one of them" % os.path.basename(folder))
    if style is None:
        raise LayoutError(folder, "this script cannot be split: keep it in %s" % os.path.basename(py_path))
    init = os.path.join(folder, "__init__.py")
    if init not in files:
        raise LayoutError(folder, "a script folder needs its __init__.py")
    raw = {}
    for p in files:
        data = read_bytes(p)
        if data is None:
            raise ChangedMeanwhile(os.path.basename(p))
        raw[p] = data
    texts = {}
    for p, data in raw.items():
        try:
            texts[p] = _norm(data)
        except UnicodeDecodeError as e:
            raise LayoutError(p, "not UTF-8 text (%s)" % e) from None
    return join(py_path, style, texts, raw)


def _owned_init_lines(lines: list[str], tree: ast.Module) -> set[int]:
    """Indexes of the lines of an __init__.py that are rpgsync's: the note on the parts,
    the imports of the parts, the class statement."""
    owned = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.level:
            owned.update(range(stmt.lineno - 1, (stmt.end_lineno or stmt.lineno)))
    for n, line in enumerate(lines):
        if line == MARK[0] or _MARK_2.fullmatch(line):
            owned.add(n)
            if line == MARK[0] and n and not lines[n - 1].strip():
                owned.add(n - 1)  # the blank line rpgsync puts above it
    return owned


def _part_body(path: str, lines: list[str], style: Style) -> tuple[str | None, str | None, int]:
    """(class, its base, index of the first line of the entries) of a part file."""
    if style.wrapped:
        classes = [n for n, line in enumerate(lines) if line.startswith("class ")]
        if len(classes) != 1:
            raise LayoutError(path, "a part holds one class (its entries), it has %d" % len(classes))
        c = classes[0]
        m = _CLASS.match(lines[c])
        if not m:
            raise LayoutError(path, "the class of a part is written `class Name(Base):`", c + 1)
        _check_header(path, lines[:c])
        for n in range(c + 1, len(lines)):
            if lines[n][:1] not in ("", " ", "\t", "#"):
                raise LayoutError(path, "a part holds only its class: this line is outside it", n + 1)
        return m.group(1), m.group(2), c + 1
    end = _header_end(lines)
    _check_header(path, lines[:end])
    return None, None, end


def _check_header(path: str, lines: list[str]) -> None:
    """The lines above the entries of a part: a docstring, comments and imports only."""
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError as e:
        raise LayoutError(path, "syntax error: %s" % e.msg, e.lineno) from None
    for n, stmt in enumerate(tree.body):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if n == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        raise LayoutError(path, "above its entries, a part holds only its docstring and imports", stmt.lineno)


def _header_end(lines: list[str]) -> int:
    """Index of the first line after the docstring and imports a map part starts with
    (comments right above the first event belong to it)."""
    n, end = 0, 0
    while n < len(lines) and not lines[n].strip():
        n += 1
    if n < len(lines) and lines[n].lstrip().startswith(('"""', "'''", '"', "'")):
        q = lines[n].lstrip()[:3] if lines[n].lstrip()[:3] in ('"""', "'''") else lines[n].lstrip()[:1]
        rest = lines[n].lstrip()[len(q) :]
        while q not in rest and n + 1 < len(lines):
            n += 1
            rest = lines[n]
        n += 1
        end = n
    while n < len(lines):
        line = lines[n]
        if not line.strip() or line.startswith("#"):
            n += 1
            continue
        if line.startswith(("from ", "import ")):
            depth = line.count("(") - line.count(")")
            n += 1
            while depth > 0 and n < len(lines):
                depth += lines[n].count("(") - lines[n].count(")")
                n += 1
            end = n
            continue
        break
    while end < len(lines) and not lines[end].strip():
        end += 1
    return end


def _strip(lines: list[str]) -> list[str]:
    a, b = 0, len(lines)
    while a < b and not lines[a].strip():
        a += 1
    while b > a and not lines[b - 1].strip():
        b -= 1
    return lines[a:b]


def join(py_path: str, style: Style, texts: dict[str, str], raw: dict[str, bytes] | None = None) -> ScriptFiles:
    """The script of a folder's files (path -> text) as one text."""
    folder = package_dir(py_path)
    init = os.path.join(folder, "__init__.py")
    init_lines = texts[init].split("\n")
    try:
        tree = ast.parse(texts[init])
    except SyntaxError as e:
        raise LayoutError(init, "syntax error: %s" % e.msg, e.lineno) from None
    owned = _owned_init_lines(init_lines, tree)

    # the parts, in the order of their names
    parts = []
    for path in sorted(p for p in texts if p != init):
        lines = texts[path].split("\n")
        cls, base, start = _part_body(path, lines, style)
        body = lines[start:]
        a = 0
        while a < len(body) and not body[a].strip():
            a += 1
        parts.append((path, cls, base, start + a + 1, _strip(body)))  # (line number of its first entry line)
    names = [cls for _, cls, _, _, _ in parts if cls]
    if len(set(names)) != len(names):
        raise LayoutError(folder, "two parts have the same class name")
    bases = {base for _, _, base, _, _ in parts if base}
    if len(bases) > 1:
        raise LayoutError(folder, "the classes of the parts have different bases: %s" % ", ".join(sorted(bases)))

    out: list[str] = []
    spans: list[tuple[int, str, int, int]] = []

    def emit(lines: list[str], path: str, line: int) -> None:  # line: of lines[0] in its file (1-based)
        if lines:
            spans.append((len(out), path, line, len(lines)))
            out.extend(lines)

    def emit_init(a: int, b: int) -> None:  # the lines [a, b) of __init__.py that are not rpgsync's
        run = a
        for n in range(a, b + 1):
            if n == b or n in owned:
                emit(init_lines[run:n], init, run + 1)
                run = n + 1

    entries = any(body for *_, body in parts)
    result_parts = []
    if style.wrapped:
        classes = [stmt for stmt in tree.body if isinstance(stmt, ast.ClassDef)]
        if len(classes) != 1:
            raise LayoutError(init, "__init__.py holds one class (made of the parts), it has %d" % len(classes))
        cdef = classes[0]
        if bases:
            base = next(iter(bases))
        elif cdef.bases and isinstance(cdef.bases[-1], ast.Name):
            base = cdef.bases[-1].id
        else:
            raise LayoutError(init, "the class must end with the base of the script's class", cdef.lineno)
        c = cdef.lineno - 1  # the class statement: up to its first body line
        first = cdef.body[0].lineno - 1 if cdef.body else c + 1
        while first > c + 1 and init_lines[first - 1].strip().startswith("#"):
            first -= 1  # comments above the first statement of the body are its
        t = next(
            (n for n in range(first, len(init_lines)) if init_lines[n][:1] not in ("", " ", "\t", "#")),
            len(init_lines),
        )
        end = t
        while end > first and not init_lines[end - 1].strip():
            end -= 1
        emit_init(0, c)
        spans.append((len(out), init, c + 1, 1))
        out.append("class %s(%s):" % (cdef.name, base))
        pre = init_lines[first:end]
        if entries and [line.strip() for line in pre] == ["pass"]:
            pre = []  # the body of a class made of parts only
        emit(pre, init, first + 1)
        lead = 1 if pre else 0
        for path, cls, _, line, body in parts:
            if not body:
                continue
            gap = lead if not result_parts else style.sep
            out.extend([""] * gap)
            result_parts.append(Part(path=path, cls=cls, first=len(out), end=len(out) + len(body)))
            emit(body, path, line)
        emit_init(end, len(init_lines))
    else:
        end = max((n + 1 for n, line in enumerate(init_lines) if line.strip() and n not in owned), default=0)
        emit_init(0, end)
        for path, cls, _, line, body in parts:
            if not body:
                continue
            out.extend([""] * style.sep)
            result_parts.append(Part(path=path, cls=cls, first=len(out), end=len(out) + len(body)))
            emit(body, path, line)
        out.append("")
    if raw is None:
        raw = {p: t.encode("utf-8") for p, t in texts.items()}
    return ScriptFiles(path=py_path, text="\n".join(out), raw=raw, package=True, parts=result_parts, spans=spans)


# --------------------------------------------------------------------------
# The entries of a text
# --------------------------------------------------------------------------


class Chunk(BaseModel):
    """An entry and the comments and blank lines above it: lines [lead, end), the
    entry itself starting at `start`."""

    lead: int
    start: int
    end: int
    id: int | None


class Layout(BaseModel):
    """A joined text cut into its header, its class statement, what the class holds
    before its entries (Config), its entries, and what follows them."""

    lines: list[str]
    header: list[str]  # up to the class statement (maps: up to the first entry)
    cls: tuple[str, str] | None  # (name, base) of the class
    pre: list[str]
    chunks: list[Chunk]
    tail: list[str]


def _is_start(line: str, style: Style) -> bool:
    if style.decorator:
        return line.startswith(style.indent + "@" + style.decorator + "(")
    return bool(_TABLE_ENTRY.match(line))


def _entry_id(lines: list[str], start: int, style: Style) -> int | None:
    if style.decorator:
        m = re.match(r"\s*@" + re.escape(style.decorator) + r"\(\s*(\d+)\s*[,)]", "\n".join(lines[start : start + 3]))
        return int(m.group(1)) if m else None
    m = _POSITIONAL_ID.match(lines[start]) or _FIRST_KEYWORD_ID.search(lines[start])
    if m is None and start + 1 < len(lines):
        m = _NEXT_LINE_ID.match(lines[start + 1])
    return int(m.group(1)) if m else None


def _leading(lines: list[str], start: int, floor: int, indent: str) -> int:
    """Index of the first of the blank lines and comments right above `start` (not
    above `floor`)."""
    k = start
    while k - 1 >= floor:
        line = lines[k - 1]
        s = line.lstrip()
        if s and not (s.startswith("#") and len(line) - len(s) <= len(indent)):
            break
        k -= 1
    return k


def layout(text: str, style: Style) -> Layout | None:
    """The parts of a joined text (maybe no entries); None without its class."""
    lines = text.split("\n")
    indent = style.indent
    if style.wrapped:
        c = next((n for n, line in enumerate(lines) if line.startswith("class ")), None)
        if c is None or not _CLASS.match(lines[c]):
            return None
        m = _CLASS.match(lines[c])
        t = next((n for n in range(c + 1, len(lines)) if lines[n][:1] not in ("", " ", "\t", "#")), len(lines))
        end = t
        while end > c + 1 and not lines[end - 1].strip():
            end -= 1
        floor = c + 1
        if not style.decorator:  # a table: its Config is not an entry
            f = floor
            while f < end and not lines[f].strip():
                f += 1
            if f < end and lines[f].startswith("    class Config"):
                f += 1
                while f < end and (not lines[f].strip() or lines[f].startswith("        ")):
                    f += 1
                floor = f
            elif f < end and re.match(r"    size\s*=", lines[f]):
                floor = f + 1
        starts = [n for n in range(floor, end) if _is_start(lines[n], style)]
        cls = (m.group(1), m.group(2))  # type: ignore[union-attr]
    else:
        end = len(lines)
        while end > 0 and not lines[end - 1].strip():
            end -= 1
        starts = [n for n in range(end) if _is_start(lines[n], style)]
        c, floor, cls = -1, 0, None
    leads = [_leading(lines, s, starts[k - 1] + 1 if k else floor, indent) for k, s in enumerate(starts)]
    chunks = [
        Chunk(lead=leads[k], start=s, end=leads[k + 1] if k + 1 < len(starts) else end, id=_entry_id(lines, s, style))
        for k, s in enumerate(starts)
    ]
    first = leads[0] if leads else end
    if style.wrapped:
        header, pre = lines[:c], _strip(lines[c + 1 : first])
    else:
        header, pre = lines[:first], []
    return Layout(lines=lines, header=header, cls=cls, pre=pre, chunks=chunks, tail=lines[end:])


def _entry_text(lay: Layout, c: Chunk) -> str:
    """An entry's lines without the blank lines above it."""
    lines = lay.lines[c.lead : c.end]
    n = 0
    while n < len(lines) and not lines[n].strip():
        n += 1
    return "\n".join(lines[n:])


def equivalent(a: str, b: str, style: Style) -> bool:
    """The same script, whatever the order of its entries and the blank lines above
    them: what splitting a script into files and joining them back may change."""
    la, lb = layout(a, style), layout(b, style)
    if la is None or lb is None:
        return a == b

    def key(lay: Layout):
        header = _strip(lay.header)
        return (header, lay.cls, _strip(lay.pre), sorted(_entry_text(lay, c) for c in lay.chunks), _strip(lay.tail))

    return key(la) == key(lb)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _imports(header: list[str]) -> list[str]:
    """The import statements of a script's header, as written there."""
    try:
        tree = ast.parse("\n".join(header))
    except SyntaxError:
        return []
    out = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)) and not (isinstance(stmt, ast.ImportFrom) and stmt.level):
            out += header[stmt.lineno - 1 : stmt.end_lineno]
    return out


def _label(py_path: str) -> str:
    folder = package_dir(py_path)
    parent = os.path.basename(os.path.dirname(folder))
    name = os.path.basename(folder)
    return "%s/%s" % (parent, name) if parent == "database" else name


def _import_line(module: str, names: list[str], line_length: int) -> list[str]:
    one = "from .%s import %s" % (module, ", ".join(names))
    if len(one) <= line_length:
        return [one]
    return ["from .%s import (" % module, *("    %s," % n for n in names), ")"]


def _class_stmt(name: str, bases: list[str], line_length: int) -> list[str]:
    one = "class %s(%s):" % (name, ", ".join(bases))
    if len(one) <= line_length:
        return [one]
    return ["class %s(" % name, *("    %s," % b for b in bases), "):"]


def _names_used(src: str) -> set[str]:
    """The names a map part uses (not as attributes)."""
    out = set()
    prev = None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME and not (prev is not None and prev.type == tokenize.OP and prev.string == "."):
                out.add(tok.string)
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                prev = tok
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return out


class _Plan(BaseModel):
    module: str
    cls: str | None
    chunks: list[int]  # indexes in the layout's chunks


def _module_name(style: Style, ids: list[int | None], taken: set[str]) -> str:
    known = [i for i in ids if i is not None]
    base = "%s_%04d_%04d" % (style.prefix, min(known), max(known)) if known else "%s_part" % style.prefix
    name, n = base, 2
    while name in taken:
        name, n = "%s_%d" % (base, n), n + 1
    return name


def _class_name(module: str) -> str:
    return module[:1].upper() + module[1:]


def _group(sizes: list[int], items: list[int], budget: int, overhead: int) -> list[list[int]]:
    """Consecutive items in groups of at most `budget` bytes (one item at least)."""
    groups: list[list[int]] = []
    cur: list[int] = []
    size = overhead
    for i in items:
        if cur and size + sizes[i] > budget:
            groups.append(cur)
            cur, size = [], overhead
        cur.append(i)
        size += sizes[i]
    if cur:
        groups.append(cur)
    return groups


def _place(lay: Layout, old: ScriptFiles, old_lay: Layout | None) -> list[_Plan]:
    """The chunks of `lay` in the parts of `old`: each entry where it was."""
    plans = [_Plan(module=os.path.basename(p.path)[:-3], cls=p.cls, chunks=[]) for p in old.parts]
    if old_lay is None or not plans:
        return plans
    part_of = []  # part of each old chunk
    for c in old_lay.chunks:
        part_of.append(next((n for n, p in enumerate(old.parts) if p.first <= c.start < p.end), 0))
    same = len(old_lay.chunks) == len(lay.chunks) and all(
        a.lead == b.lead and a.start == b.start and (a.id is None or a.id == b.id)
        for a, b in zip(old_lay.chunks, lay.chunks)
    )
    if same:  # the same entries at the same lines (ids written in, labels updated...)
        for n in range(len(lay.chunks)):
            plans[part_of[n]].chunks.append(n)
        return plans
    where: dict[int, tuple[int, int]] = {}  # id -> (part, rank in the part)
    rank = [0] * len(plans)
    for n, c in enumerate(old_lay.chunks):
        if c.id is not None and c.id not in where:
            where[c.id] = (part_of[n], rank[part_of[n]])
            rank[part_of[n]] += 1
    placed: dict[int, int] = {}  # chunk -> part
    known = sorted(
        (where[c.id] + (n,) for n, c in enumerate(lay.chunks) if c.id is not None and c.id in where),
    )
    seen_ids: set[int] = set()
    for p, _, n in known:
        if lay.chunks[n].id in seen_ids:
            continue
        seen_ids.add(lay.chunks[n].id)  # type: ignore[arg-type]
        plans[p].chunks.append(n)
        placed[n] = p
    for n in range(len(lay.chunks)):
        if n in placed:
            continue
        if n and n - 1 in placed:  # after the entry before it
            p = placed[n - 1]
            plans[p].chunks.insert(plans[p].chunks.index(n - 1) + 1, n)
        else:
            p = 0
            plans[0].chunks.insert(0, n)
        placed[n] = p
    return plans


def plan(
    py_path: str,
    style: Style | None,
    text: str,
    old: ScriptFiles | None,
    max_kb: int,
    line_length: int = LINE_LENGTH,
) -> dict[str, str] | None:
    """The files to write the script as: path -> text, None for one file (py_path)."""
    if style is None:
        return None
    limit = max_kb * KB
    folder = old is not None and old.package
    if not folder and (not max_kb or len(text.encode("utf-8")) <= limit):
        return None
    lay = layout(text, style)
    if lay is None and folder:
        raise LayoutError(package_dir(py_path), "internal error: no class to split this script into")
    if lay is None or (not folder and len(lay.chunks) < 2):
        return None  # no entries to split it into
    pkg = package_dir(py_path)
    label = _label(py_path)
    imports = _imports(lay.header)
    head = ['"""Part of %s: see __init__.py"""' % label, ""] + imports
    if style.self_import and style.self_import not in imports:
        head.append(style.self_import)
    sizes = [len("\n".join(lay.lines[c.lead : c.end]).encode("utf-8")) + 1 for c in lay.chunks]
    overhead = len("\n".join(head).encode("utf-8")) + 60
    budget = int(limit * FILL) if max_kb else 1 << 60

    if folder:
        assert old is not None
        plans = _place(lay, old, layout(old.text, style))
        if not plans:
            plans = [
                _Plan(module="", cls=None, chunks=g)
                for g in _group(sizes, list(range(len(lay.chunks))), budget, overhead)
            ]
    else:
        plans = [
            _Plan(module="", cls=None, chunks=g) for g in _group(sizes, list(range(len(lay.chunks))), budget, overhead)
        ]
    # cut the parts grown over the limit
    out_plans: list[_Plan] = []
    for p in plans:
        if not p.chunks:
            continue  # emptied: removed
        total = overhead + sum(sizes[n] for n in p.chunks)
        if max_kb and total > limit and len(p.chunks) > 1:
            groups = _group(sizes, p.chunks, budget, overhead)
            out_plans.append(_Plan(module=p.module, cls=p.cls, chunks=groups[0]))
            out_plans += [_Plan(module="", cls=None, chunks=g) for g in groups[1:]]
        else:
            out_plans.append(p)
    taken = {p.module for p in out_plans if p.module}
    for p in out_plans:
        if not p.module:
            p.module = _module_name(style, [lay.chunks[n].id for n in p.chunks], taken | {"__init__"})
            taken.add(p.module)
        if style.wrapped and not p.cls:
            p.cls = _class_name(p.module)
    out_plans.sort(key=lambda p: p.module)

    files: dict[str, str] = {}
    bodies = []
    for p in out_plans:
        body: list[str] = []
        for k, n in enumerate(p.chunks):
            c = lay.chunks[n]
            lines = lay.lines[c.lead : c.end]
            if k == 0:
                while lines and not lines[0].strip():
                    lines = lines[1:]
            body += lines
        bodies.append(_strip(body) if body else body)
    refs: list[list[str]] = [[] for _ in out_plans]
    if style.event_refs:
        defined = [{m.group(1) for line in body if (m := _TOP_DEF.match(line))} for body in bodies]
        owner = {name: k for k, names in enumerate(defined) for name in names}
        for k, body in enumerate(bodies):
            used = _names_used("\n".join(body) + "\n")
            by_part: dict[int, list[str]] = {}
            for name in sorted(used):
                if owner.get(name, k) != k:
                    by_part.setdefault(owner[name], []).append(name)
            for j in sorted(by_part):
                refs[k] += _import_line(out_plans[j].module, by_part[j], line_length)
    for k, p in enumerate(out_plans):
        lines = head + refs[k] + ["", ""]
        if style.wrapped:
            lines.append("class %s(%s):" % (p.cls, lay.cls[1]))  # type: ignore[index]
        lines += bodies[k] or ([style.indent + "pass"] if style.wrapped else [])
        files[os.path.join(pkg, p.module + ".py")] = "\n".join(lines) + "\n"

    header = list(lay.header)
    gap: list[str] = []
    while header and not header[-1].strip():
        gap.append(header.pop())
    block = ["", MARK[0], MARK[1] % max_kb if max_kb else MARK[1] % MAX_FILE_KB]
    if style.wrapped:
        block += ["from .%s import %s" % (p.module, p.cls) for p in out_plans]
        name, base = lay.cls  # type: ignore[misc]
        init = header + block + (gap or ["", ""])
        init += _class_stmt(name, [p.cls for p in out_plans] + [base], line_length)  # type: ignore[misc]
        init += lay.pre or [style.indent + "pass"]
        init += lay.tail
    else:
        block += ["from . import %s" % p.module for p in out_plans]
        init = header + block + [""]
    files[os.path.join(pkg, "__init__.py")] = "\n".join(init) if init[-1] == "" else "\n".join(init) + "\n"
    return files


def write(
    py_path: str,
    style: Style | None,
    text: str,
    old: ScriptFiles | None,
    max_kb: int,
    backup: Callable[[str], None],
    line_length: int = LINE_LENGTH,
) -> ScriptFiles:
    """Write a script (one file, or the files of its folder when it is one or the text
    is over the limit): only the files that change, each backed up first.  `old`: the
    script as read before (ChangedMeanwhile if its files changed since).  -> the script
    as now written (its text may differ from `text` by blank lines above entries)."""
    check_unchanged(py_path, old)
    files = plan(py_path, style, text, old, max_kb, line_length)
    if files is not None:
        assert style is not None
        written = join(py_path, style, files)
        if not equivalent(written.text, text, style):
            if old is not None and old.package:
                raise LayoutError(package_dir(py_path), "internal error: rpgsync cannot split this script back")
            files = None  # write it as one file
    if files is None:
        data = text.encode("utf-8")
        if read_bytes(py_path) != data:
            os.makedirs(os.path.dirname(py_path), exist_ok=True)
            backup(py_path)
            write_atomic(py_path, data)
        return ScriptFiles(path=py_path, text=text, raw={py_path: data})
    folder = package_dir(py_path)
    os.makedirs(folder, exist_ok=True)
    raw: dict[str, bytes] = {}
    init = os.path.join(folder, "__init__.py")
    current = {p: (old.raw.get(p) if old is not None else None) for p in files}
    # new parts first, then the changed ones, then __init__.py, then the stale ones go:
    # each step leaves a folder that reads as an error at worst, never as fewer entries
    order = sorted(files, key=lambda p: (p == init, current[p] is not None, p))
    for p in order:
        data = files[p].encode("utf-8")
        before = current[p]
        if before is not None and _norm(before) == files[p]:
            raw[p] = before  # the same text (maybe with other line endings): left alone
            continue
        if before is not None:
            backup(p)
        write_atomic(p, data)
        raw[p] = data
    for p in sorted(old.raw if old is not None else ()):
        if p not in files and os.path.exists(p):
            backup(p)
            os.remove(p)
    written.raw = raw
    return written


def relayout_due(found: ScriptFiles, style: Style | None, max_kb: int) -> bool:
    """Is a file of the script over the limit (a script to split, a part to cut)?"""
    if style is None or not max_kb:
        return False
    limit = max_kb * KB
    return any(len(data) > limit for p, data in found.raw.items() if not p.endswith("__init__.py"))


def entries_lost(found: ScriptFiles, style: Style | None, ids: set[int | None]) -> list[tuple[str, int, int]]:
    """The entries the parts of a folder show that a compile of its joined text did not
    read (a bracket or a quote left open at the end of a part swallows the next ones):
    [(file, line, id)]."""
    if not found.package or style is None:
        return []
    lay = layout(found.text, style)
    if lay is None:
        return []
    out = []
    for c in lay.chunks:
        if c.id is not None and c.id not in ids:
            path, line = found.where(c.start + 1)
            out.append((path, line or 0, c.id))
    return out
