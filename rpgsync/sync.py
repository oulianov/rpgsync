"""Two-way synchronisation between game data files and event scripts.

For every *unit* (one map file <-> one script, or the database's common
events <-> CommonEvents.py) the last synchronised state is remembered in
``Scripts/.rpgsync``: the hashes of both files and a copy of the script text
("base").  On each sync:

* only the game file changed   -> the script is regenerated
* only the script changed      -> it is compiled and patched into the game file
* both changed                 -> three-way merge per event, using the base
                                  as common ancestor; real conflicts keep the
                                  game version and save the script version
                                  next to it as ``*.conflict.py``

Game files are patched, never rebuilt: everything the script does not
describe (tiles, map settings, the rest of the database) and every event
the script did not change stay byte-for-byte identical.  Every overwritten
file is backed up to ``Scripts/.rpgsync/history`` first.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable

from pydantic import BaseModel

from . import script as S
from .commands import CompileError, TableSize
from .compat import check_specs, load_rules, target_engine
from .lcf import LcfError, LcfFile
from .project import Project

Log = Callable[[str], None]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


class ChangedMeanwhile(Exception):
    """A file changed while rpgsync was syncing it (an edit in the editor or in the
    script): nothing more is written, the next pass syncs both changes."""


def check_unchanged(path: str, expected: bytes | None) -> None:
    if read_bytes(path) != expected:
        raise ChangedMeanwhile(os.path.basename(path))


class ScriptError(Exception):
    def __init__(self, path: str, err: CompileError):
        self.path = path
        self.err = err
        line = ":%d" % err.lineno if err.lineno else ""
        super().__init__("%s%s: %s" % (path, line, err))


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


class Unit:
    """A game data file paired with a script file."""

    name: str
    bin_path: str
    py_path: str
    kind: str

    def __init__(self, project: Project):
        self.project = project
        self._bin_specs: tuple[bytes, list] | None = None

    # implemented by subclasses
    def decompile(self, data: bytes) -> str: ...
    def compile(self, text: str) -> list: ...
    def specs_from_bin(self, data: bytes) -> list: ...
    def apply(self, data: bytes, specs: list) -> tuple[bytes, dict[str, list[int]]]: ...

    def fingerprint(self, data: bytes | None) -> str:
        """Hash of the part of the game file this unit covers."""
        return sha(data)

    def bin_specs(self, data: bytes) -> list:
        """specs_from_bin, remembered for the same game data (a sync reads them more
        than once, and decoding every command of a big database is slow)."""
        if self._bin_specs is None or self._bin_specs[0] is not data:
            self._bin_specs = (data, self.specs_from_bin(data))
        return self._bin_specs[1]

    def existing_ids(self, data: bytes) -> list[int]:
        """Ids a new entry must not take."""
        return [s.id for s in self.bin_specs(data)]

    def bin_specs_for(self, data: bytes, ids: set) -> list:
        """The game's specs of these ids only."""
        return [s for s in self.bin_specs(data) if s.id in ids]

    def compile_incremental(self, text: str, base: str) -> list | None:
        """Specs of a script edited since the last sync (`base`), compiling only the
        entries whose source changed (KeepSpec for the others); None: compile all."""
        return None

    format_problem: str | None = None  # why the last generated script could not be formatted

    def formatted(self, text: str) -> str:
        """ruff-formatted text (with the scripts folder's ruff settings), if it
        compiles to exactly the same events."""
        from .fmt import format_source

        reference = [s.key() for s in self.compile(text)]

        def same(t: str) -> bool:
            try:
                return [s.key() for s in self.compile(t)] == reference
            except CompileError:
                return False

        text, self.format_problem = format_source(text, same, self.py_path, self.project.script_dir)
        return text

    def compile_checked(self, text: str, data: bytes | None = None, base: str | None = None) -> tuple[list, list]:
        """Compile a script; assign ids to new entries.  -> (specs, [(spec, id)])
        base: the script as of the last sync, the game unchanged since: only the
        entries edited since are compiled."""
        try:
            specs = self.compile_incremental(text, base) if base is not None else None
            if specs is None:
                specs = self.compile(text)
        except CompileError as e:
            raise ScriptError(self.py_path, e) from None
        needs_ids = any(getattr(s, "id", 0) is None for s in specs)
        existing = self.existing_ids(data) if data is not None and needs_ids else []
        assigned = S.assign_ids(specs, existing)
        return specs, assigned


class MapUnit(Unit):
    kind = "map"

    def __init__(self, project: Project, map_id: int, path: str):
        super().__init__(project)
        self.map_id = map_id
        self.name = "Map%04d" % map_id
        self.bin_path = path
        self.py_path = project.script_for_map(map_id)

    def title(self) -> str:
        name = self.project.map_names().get(self.map_id)
        return "%s%s" % (self.name, " - " + name if name else "")

    def decompile(self, data):
        f = LcfFile.parse(data)
        text = S.decompile_map(f.root, self.project.context(), self.title(), os.path.basename(self.bin_path))
        return self.formatted(text)

    def compile(self, text):
        return S.compile_map_source(text, self.project.context(), self.py_path)

    def compile_incremental(self, text, base):
        part = S.incremental_source(text, base, "event", "")
        if part is None:
            return None
        src, changed, unchanged = part
        specs = S.compile_map_source(src, self.project.context(), self.py_path, names_src=text)
        if sorted(s.id for s in specs) != sorted(changed):
            return None  # not the events we expected: compile everything
        return specs + [S.KeepSpec(id=eid) for eid in unchanged]

    def specs_from_bin(self, data):
        return S.map_event_specs(LcfFile.parse(data).root, self.project.context())

    def bin_specs_for(self, data, ids):
        return S.map_event_specs(LcfFile.parse(data).root, self.project.context(), ids=set(ids))

    def apply(self, data, specs):
        f = LcfFile.parse(data)
        summary = S.apply_events(f.root, specs)
        return f.to_bytes(), summary


class CommonEventsUnit(Unit):
    kind = "common"
    name = "CommonEvents"

    def __init__(self, project: Project):
        super().__init__(project)
        self.bin_path = project.ldb_path
        self.py_path = project.common_events_script

    def decompile(self, data):
        text = S.decompile_common_events(LcfFile.parse(data).root, self.project.context(), "RPG_RT.ldb")
        return self.formatted(text)

    def compile(self, text):
        return S.compile_common_events_source(text, self.project.context(), self.py_path)

    def compile_incremental(self, text, base):
        part = S.incremental_source(text, base, "common_event", "    ")
        if part is None:
            return None
        src, changed, unchanged = part
        specs = self.compile(src)
        if sorted(s.id for s in specs if isinstance(s, S.CommonEventSpec)) != sorted(changed):
            return None  # not the entries we expected: compile everything
        return specs + [S.KeepSpec(id=eid) for eid in unchanged]

    def specs_from_bin(self, data):
        root = LcfFile.parse(data).root
        return S.common_event_specs(root) + [TableSize(size=len(root.get("commonevents")))]

    def bin_specs_for(self, data, ids):
        items = {it.id: it for it in LcfFile.parse(data).root.get("commonevents")}
        return [S._ce_spec(items[i]) for i in ids if i in items]

    def fingerprint(self, data):
        return _chunk_sha(data, "commonevents")

    def apply(self, data, specs):
        f = LcfFile.parse(data)
        summary = S.apply_common_events(f.root, specs)
        return f.to_bytes(), summary


class DatabaseUnit(Unit):
    """One database table (actors, items, enemies...) <-> Scripts/database/<table>.py"""

    kind = "database"

    def __init__(self, project: Project, table: str):
        super().__init__(project)
        self.table = table
        self.name = "database/" + table
        self.bin_path = project.ldb_path
        self.py_path = os.path.join(project.script_dir, "database", table + ".py")
        self.new_names: dict[int, str] = {}

    def decompile(self, data):
        from . import database

        # attribute names come from the current file: they belong to the scripts
        keep = dict(self.new_names)
        text = read_bytes(self.py_path)
        if text is not None:
            keep.update(database.read_identifiers(text.decode("utf-8", "replace")))
        # entries with a list longer than a line (stat curves...) are marked
        # `# fmt: skip`: ruff would put each of their values on its own line
        from .fmt import line_length

        root = LcfFile.parse(data).root
        text = database.export_tables(
            root,
            self.project.context(),
            keep={self.table: keep},
            skip_width=line_length(self.project.script_dir),
            tables=[self.table],  # this unit's table only
        )[self.table]
        return self.formatted(text)

    def compile_checked(self, text, data=None, base=None):
        specs, assigned = super().compile_checked(text, data, base)
        # entries that just got an id keep the attribute name they were given
        self.new_names = {new_id: s._ident for s, new_id in assigned if getattr(s, "_ident", None)}
        return specs, assigned

    def compile(self, text):
        from . import database

        entries: list = database.compile_table(self.table, text, self.project.context(), self.py_path)
        size = database.table_size(text)
        return entries + ([size] if size is not None else [])

    def specs_from_bin(self, data):
        from . import database

        root = LcfFile.parse(data).root
        entries = database.table_entries_from_bin(root, self.table, self.project.context())
        return entries + [TableSize(size=len(root.get(self.table)))]

    def fingerprint(self, data):
        return _chunk_sha(data, self.table)

    def existing_ids(self, data):
        # new entries go after the last slot: empty slots may still be referenced
        root = LcfFile.parse(data).root
        return list(range(1, len(root.get(self.table)) + 1))

    def apply(self, data, specs):
        from . import database

        f = LcfFile.parse(data)
        summary = database.apply_table(f.root, self.table, specs, self.project.context())
        return f.to_bytes(), summary


# Version of the script layout.  Scripts written by an older rpgsync are
# regenerated once (when they have no unsynced edits) so they get the new layout.
FORMAT = 4


def _chunk_sha(data: bytes | None, field: str) -> str:
    """Hash of one chunk of the database, so that each table (and the common
    events) only sees its own changes in the shared RPG_RT.ldb."""
    if data is None:
        return "missing"
    root = LcfFile.parse(data).root
    chunk = root._chunk(root._field(field)[0])
    return sha(chunk.raw if chunk is not None else b"")


def insert_ids(text: str, assigned, decorator: str) -> str:
    """Write auto-assigned ids into the decorators of new events."""
    if not assigned:
        return text
    tree = ast.parse(text)
    edits = []
    by_line = {spec.lineno: new_id for spec, new_id in assigned}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.lineno in by_line:
            for d in stmt.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == decorator:
                    sep = ", " if (d.args or d.keywords) else ""
                    edits.append((d.func.end_lineno, d.func.end_col_offset, "%d%s" % (by_line[stmt.lineno], sep)))
    lines = text.split("\n")
    for lineno, col, ins in sorted(edits, reverse=True):
        # col_offset is in UTF-8 bytes
        raw = lines[lineno - 1].encode("utf-8")
        pos = col + 1  # after "("
        lines[lineno - 1] = (raw[:pos] + ins.encode("utf-8") + raw[pos:]).decode("utf-8")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


class State:
    def __init__(self, project: Project):
        self.project = project
        self.path = os.path.join(project.state_dir, "state.json")
        self.base_dir = os.path.join(project.state_dir, "base")  # created on the first record
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, unit: Unit) -> dict | None:
        return self.data.get(unit.name)

    def record(self, unit: Unit, bin_data: bytes, py_text: str) -> None:
        self.data[unit.name] = {
            "bin": unit.fingerprint(bin_data),
            "py": sha(py_text.encode("utf-8")),
            "ctx": self.project.context_signature(),
            "format": FORMAT,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.project.ensure_state_dir()
        base = os.path.join(self.base_dir, unit.name + ".py")
        os.makedirs(os.path.dirname(base), exist_ok=True)
        with open(base, "w", encoding="utf-8") as f:
            f.write(py_text)
        self.save()

    def base_text(self, unit: Unit) -> str | None:
        try:
            with open(os.path.join(self.base_dir, unit.name + ".py"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def save(self) -> None:
        self.project.ensure_state_dir()
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------
# Syncer
# --------------------------------------------------------------------------


class Result(BaseModel):
    unit: str
    action: str  # "none", "export", "import", "merge", "error", "conflict"
    message: str = ""
    seconds: float | None = None  # how long the sync took, set by the caller
    retry: bool = False  # a file changed during the sync: sync it again


class Syncer:
    def __init__(self, project: Project, log: Log = print, keep_history: int = 50):
        self.project = project
        self.state = State(project)
        self.log = log
        self.keep_history = keep_history
        self.history_dir = os.path.join(project.state_dir, "history")
        self._failed: dict[str, str] = {}  # unit -> py hash that failed to compile
        self._tables: tuple[tuple | None, list[str]] = (None, [])  # RPG_RT.ldb stat -> its tables

    def units(self) -> list[Unit]:
        from . import database

        out: list[Unit] = [CommonEventsUnit(self.project)]
        stat = _stat(self.project.ldb_path)
        if stat != self._tables[0]:  # parse the database only when it changes
            db = read_bytes(self.project.ldb_path)
            try:
                root = LcfFile.parse(db).root if db is not None else None
            except LcfError:
                root = None  # being written by the editor: keep the tables known so far, read it again later
                stat = self._tables[0]
            if root is not None or db is None:
                tables = [t for t in database.TABLES if root is not None and database.has_table(root, t)]
                self._tables = (stat, tables)
        out += [DatabaseUnit(self.project, t) for t in self._tables[1]]
        for map_id, path in self.project.map_paths().items():
            out.append(MapUnit(self.project, map_id, path))
        return out

    # -- helpers -------------------------------------------------------------
    def backup(self, path: str) -> None:
        if not os.path.exists(path):
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        d = os.path.join(self.history_dir, stamp)
        os.makedirs(d, exist_ok=True)
        shutil.copy2(path, os.path.join(d, os.path.basename(path)))
        snaps = sorted(os.listdir(self.history_dir))
        for old in snaps[: -self.keep_history]:
            shutil.rmtree(os.path.join(self.history_dir, old), ignore_errors=True)

    def write_script(self, unit: Unit, text: str, expect: bytes | None = None) -> None:
        """expect: the script as the sync read it; ChangedMeanwhile if it was edited since."""
        check_unchanged(unit.py_path, expect)
        if unit.format_problem:
            self.log("%s: written unformatted: %s" % (os.path.basename(unit.py_path), unit.format_problem))
            unit.format_problem = None
        if read_bytes(unit.py_path) == text.encode("utf-8"):
            return
        os.makedirs(os.path.dirname(unit.py_path), exist_ok=True)
        self.backup(unit.py_path)
        write_atomic(unit.py_path, text.encode("utf-8"))

    def write_bin(self, unit: Unit, data: bytes, expect: bytes | None) -> None:
        """expect: the game file as the sync read it; ChangedMeanwhile if it was saved since."""
        # never write something we cannot read back identically
        if LcfFile.parse(data).to_bytes() != data:
            raise RuntimeError("internal error: generated %s does not re-read identically" % unit.bin_path)
        check_unchanged(unit.bin_path, expect)
        self.backup(unit.bin_path)
        write_atomic(unit.bin_path, data)
        if unit.kind == "common":
            self.project.context(refresh=True)

    @staticmethod
    def describe(summary: dict[str, list[int]], what: str) -> str:
        parts = []
        if summary.get("size"):
            parts.append("size %d -> %d" % tuple(summary["size"]))
        for k in ("changed", "added", "removed"):
            if summary.get(k):
                note = " (emptied: database ids cannot have holes)" if k == "removed" and what == "common event" else ""
                parts.append("%s %s %s%s" % (k, what, ", ".join(map(str, summary[k])), note))
        return "; ".join(parts) or "no changes"

    def check_engine(self, unit: Unit, specs: list, data: bytes) -> list[str]:
        """Engine compatibility problems of the entries the script changes.

        Entries identical to the game file are not checked, so a game that
        already uses patch commands can still be edited."""
        if unit.kind not in ("map", "common"):
            return []  # database entries have no event commands
        compiled = [s for s in specs if not isinstance(s, S.KeepSpec)]
        if len(compiled) < len(specs):  # incremental compile: compare the edited entries only
            unchanged = [s.key() for s in unit.bin_specs_for(data, {s.id for s in compiled})]
        else:
            unchanged = [s.key() for s in unit.bin_specs(data)]
        changed = [s for s in compiled if s.key() not in unchanged]
        if not changed:
            return []
        patches, allow = load_rules(self.project.script_dir)
        engine = target_engine(self.project.script_dir, self.project.context().engine)
        return check_specs(changed, engine, patches, allow)

    # -- main entry ------------------------------------------------------------
    def sync(self, unit: Unit, prefer: str | None = None) -> Result:
        """prefer: None (automatic), "game" (export) or "script" (import)."""
        try:
            return self._sync(unit, prefer)
        except ChangedMeanwhile as e:
            # the game file or the script was saved while we worked on the old version
            return Result(
                unit=unit.name, action="none", message="%s changed during the sync: syncing again" % e, retry=True
            )
        except (LcfError, OSError) as e:
            # a game file read while the editor writes it, a file locked by another program (Windows)...
            return Result(unit=unit.name, action="none", message="cannot sync yet (%s): trying again" % e, retry=True)

    def _sync(self, unit: Unit, prefer: str | None) -> Result:
        what = {"map": "event", "common": "common event"}.get(unit.kind, "entry")
        data = read_bytes(unit.bin_path)
        if data is None:
            return Result(unit=unit.name, action="none", message="game file missing")
        raw = read_bytes(unit.py_path)  # compared before writing: an edit made meanwhile is never overwritten
        text = None if raw is None else raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        st = self.state.get(unit)

        if text is None or prefer == "game":
            new_text = unit.decompile(data)
            if text != new_text:
                self.write_script(unit, new_text, raw)
            self.state.record(unit, data, new_text)
            return Result(
                unit=unit.name,
                action="export",
                message="wrote %s" % os.path.relpath(unit.py_path, self.project.game_dir),
            )

        bin_changed = st is None or unit.fingerprint(data) != st["bin"]
        py_hash = sha(text.encode("utf-8"))
        py_changed = st is None or py_hash != st["py"]
        if prefer == "script":
            bin_changed, py_changed = False, True
        outdated = st is not None and st.get("format") != FORMAT  # written by an older rpgsync
        if not bin_changed and not py_changed and not outdated:
            return Result(unit=unit.name, action="none")

        if outdated and not py_changed and not bin_changed and prefer != "script":
            new_text = unit.decompile(data)
            self.write_script(unit, new_text, raw)
            self.state.record(unit, data, new_text)
            return Result(
                unit=unit.name,
                action="export" if new_text != text else "none",
                message="regenerated %s in the new rpgsync layout" % os.path.basename(unit.py_path),
            )
        if bin_changed and not py_changed:
            new_text = unit.decompile(data)
            self.write_script(unit, new_text, raw)
            self.state.record(unit, data, new_text)
            self._failed.pop(unit.name, None)
            return Result(
                unit=unit.name,
                action="export",
                message="%s changed in the editor -> regenerated %s"
                % (os.path.basename(unit.bin_path), os.path.basename(unit.py_path)),
            )

        if self._failed.get(unit.name) == py_hash and prefer is None:
            return Result(unit=unit.name, action="none")  # same broken script as before; already reported
        base = None
        if st is not None and not bin_changed and prefer is None and st.get("ctx") == self.project.context_signature():
            # the game is as the last sync left it: compile only what was edited since
            base = self.state.base_text(unit)
            if base is not None and sha(base.encode("utf-8")) != st["py"]:
                base = None
        try:
            specs, assigned = unit.compile_checked(text, data, base)
        except ScriptError as e:
            self._failed[unit.name] = py_hash
            return Result(unit=unit.name, action="error", message=str(e))
        problems = self.check_engine(unit, specs, data)
        if problems:
            self._failed[unit.name] = py_hash
            return Result(
                unit=unit.name,
                action="error",
                message="%s does not fit the engine, game not written:\n  %s"
                % (os.path.basename(unit.py_path), "\n  ".join(problems)),
            )
        self._failed.pop(unit.name, None)

        if st is None and prefer is None:
            # Never synced: only adopt the pair if it already agrees.
            if [s.key() for s in specs] == [s.key() for s in unit.bin_specs(data)]:
                self.state.record(unit, data, text)
                return Result(unit=unit.name, action="none", message="already in sync")
            return Result(
                unit=unit.name,
                action="conflict",
                message="%s and %s differ and were never synced; run `rpgsync push` (script wins) "
                "or `rpgsync pull --force` (game wins)"
                % (os.path.basename(unit.py_path), os.path.basename(unit.bin_path)),
            )

        conflicts: list[int] = []
        if bin_changed and py_changed:
            specs, conflicts = self.merge(unit, specs, data)

        try:
            new_data, summary = unit.apply(data, specs)
        except ValueError as e:  # e.g. size lowered below an entry still in use
            self._failed[unit.name] = py_hash
            return Result(unit=unit.name, action="error", message="%s: %s" % (unit.py_path, e))
        if new_data != data:
            self.write_bin(unit, new_data, data)
        if assigned and not conflicts and unit.kind != "database":
            text = insert_ids(text, assigned, "event" if unit.kind == "map" else "common_event")
            check_unchanged(unit.py_path, raw)
            write_atomic(unit.py_path, text.encode("utf-8"))
            raw = text.encode("utf-8")
        if summary.get("size") and not (bin_changed and py_changed):
            # an id past the end grew the table: show the new size in the script
            new_text = re.sub(r"^(\s*)size = \d+", r"\g<1>size = %d" % summary["size"][1], text, count=1, flags=re.M)
            if new_text != text:
                text = new_text
                check_unchanged(unit.py_path, raw)
                write_atomic(unit.py_path, text.encode("utf-8"))
                raw = text.encode("utf-8")
        if bin_changed and py_changed:
            # the merged result is the new truth for both sides
            merged_text = unit.decompile(new_data)
            if conflicts:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                cpath = unit.py_path[:-3] + ".conflict-%s.py" % stamp
                write_atomic(cpath, text.encode("utf-8"))
            self.write_script(unit, merged_text, raw)
            self.state.record(unit, new_data, merged_text)
            msg = "both sides changed -> merged (%s)" % self.describe(summary, what)
            if conflicts:
                msg += "; CONFLICT on %s %s: kept the editor version, your script version is in %s" % (
                    what,
                    ", ".join(map(str, conflicts)),
                    os.path.basename(cpath),
                )
                return Result(unit=unit.name, action="conflict", message=msg)
            return Result(unit=unit.name, action="merge", message=msg)
        if unit.kind == "database" and (summary.get("added") or summary.get("removed") or assigned):
            # show new entries with their id and the editor's defaults for omitted fields
            canonical = unit.decompile(new_data)
            if assigned or [s.key() for s in unit.compile(canonical)] != [s.key() for s in specs]:
                self.write_script(unit, canonical, raw)
                text = canonical
        self.state.record(unit, new_data, text)
        msg = "%s -> %s: %s" % (
            os.path.basename(unit.py_path),
            os.path.basename(unit.bin_path),
            self.describe(summary, what),
        )
        if assigned:
            msg += " (new ids: %s)" % ", ".join(str(i) for _, i in assigned)
        return Result(unit=unit.name, action="import", message=msg)

    def merge(self, unit: Unit, script_specs: list, data: bytes) -> tuple[list, list[int]]:
        base_text = self.state.base_text(unit)
        try:
            base = {s.id: s for s in unit.compile(base_text)} if base_text else {}
        except CompileError:
            base = {}
        mine = {s.id: s for s in script_specs}
        theirs = {s.id: s for s in unit.bin_specs(data)}

        def k(spec):
            return None if spec is None else spec.key()

        out, conflicts = [], []
        for eid in sorted(set(base) | set(mine) | set(theirs)):
            b, m, t = base.get(eid), mine.get(eid), theirs.get(eid)
            if k(m) == k(b):
                pick = t
            elif k(t) == k(b) or k(m) == k(t):
                pick = m
            else:
                pick = t
                conflicts.append(eid)
            if pick is not None:
                out.append(pick)
        return out, conflicts

    # -- watch ----------------------------------------------------------------
    def watch(self, interval: float = 0.5, announce: bool = True, first_pass: bool = True) -> None:
        """Sync every unit, then each one whose files change, until interrupted.
        announce=False: the caller prints its own "Watching ..." line;
        first_pass=False: the caller already synced every unit."""
        if announce:
            self.log("Watching %s" % self.project.game_dir)
            self.log("Every edit will be synced between python files and game files.")
            self.log("Ctrl+C to stop")
        seen: dict[str, tuple] = {}
        pending: dict[str, tuple] = {}
        retried: dict[str, str | None] = {}  # unit -> message of its last retry
        first = first_pass
        if not first_pass:
            for unit in self.units():
                seen[unit.name] = (_stat(unit.bin_path), _stat(unit.py_path))
        while True:
            units = self.units()
            for unit in units:
                sig = (_stat(unit.bin_path), _stat(unit.py_path))
                if first or seen.get(unit.name) != sig:
                    # debounce: act once a file has been stable for one interval
                    if pending.get(unit.name) == sig or first:
                        start = time.perf_counter()
                        r = self.sync(unit)
                        r.seconds = time.perf_counter() - start
                        if not (r.retry and retried.get(unit.name) == r.message):
                            self.report(r)  # a retry reported once, not on every pass
                        retried[unit.name] = r.message if r.retry else None
                        sig = (_stat(unit.bin_path), _stat(unit.py_path))
                        if r.retry:
                            seen.pop(unit.name, None)  # sync it again once its files settle
                        else:
                            seen[unit.name] = sig
                        pending.pop(unit.name, None)
                    else:
                        pending[unit.name] = sig
            first = False
            time.sleep(interval)

    def report(self, r: Result) -> None:
        if r.action == "none" and not r.message:
            return
        stamp = time.strftime("%H:%M:%S")
        tag = {"error": "ERROR ", "conflict": "CONFLICT "}.get(r.action, "")
        msg = r.message or r.action
        if r.seconds is not None and r.action != "none":
            # after the first line: multi-line messages list details below it
            first, sep, rest = msg.partition("\n")
            msg = "%s (%.2f s)%s%s" % (first, r.seconds, sep, rest)
        self.log("[%s] %s%s: %s" % (stamp, tag, r.unit, msg))


def _stat(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None
