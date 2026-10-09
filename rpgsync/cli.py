"""Command line interface.

cd /path/to/MyGame
rpgsync                # keep game and scripts in sync (live); sets the scripts up the first time
rpgsync status         # what is pending, on which side
rpgsync pull           # game -> scripts
rpgsync push           # scripts -> game
rpgsync check          # compile, engine and type checks, without writing
rpgsync clean          # delete the backups of overwritten files

Every command works on the game of the current folder (the folder holding
RPG_RT.ldb, its Scripts folder, or any folder inside them), or on the game
or scripts folder given as argument.
"""

from __future__ import annotations

import difflib
import io
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.text import Text

from .compat import check_specs, load_rules, target_engine
from .project import Project
from .scaffold import init_scripts_project
from .sync import Result, ScriptError, Syncer, Unit, read_bytes, sha

app = typer.Typer(
    help="A python twin of your RPG Maker 2000/2003 game.",
    no_args_is_help=True,
    add_completion=False,
)

ProjectArg = Annotated[
    str | None,
    typer.Argument(
        help="The game folder (with RPG_RT.ldb) or its scripts folder [default: the current folder].",
        show_default=False,
    ),
]
MapOpt = Annotated[list[int] | None, typer.Option("--map", help="Only this map id (repeatable).")]
NoCommonOpt = Annotated[bool, typer.Option("--no-common", help="Skip database/common_events.py.")]

ENGINES = {"2k": "RPG Maker 2000", "2k3": "RPG Maker 2003"}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

# one marker at the start of a line: an emoji, or plain ASCII where the
# console cannot show emojis (Windows cmd.exe with cp437/cp850/cp1252...)
MARKERS = {
    "ok": ("\u2705", "[ok]"),  # check mark
    "script": ("\U0001f4dd", "[s]"),  # memo: a script was written / changed
    "game": ("\U0001f3ae", "[g]"),  # video game: a game file was written / changed
    "merge": ("\U0001f500", "[~]"),  # twisted arrows
    "conflict": ("\u26a0\ufe0f", "[!]"),  # warning
    "error": ("\u274c", "[x]"),  # cross mark
    "info": ("\u2139\ufe0f", "[i]"),  # information
    "watch": ("\U0001f440", "[*]"),  # eyes
    "new": ("\u2728", "[+]"),  # sparkles
    "file": ("\U0001f4c4", "[+]"),  # page
    "delete": ("\U0001f5d1\ufe0f", "[-]"),  # wastebasket
    "stop": ("\U0001f44b", "[.]"),  # waving hand
}

# file names in sync messages, shown in cyan
FILE_NAMES = r"[\w./\\-]+\.(?:py|lmu|ldb)\b"


def _never_fail(stream) -> None:
    """Characters the console cannot show become "?" instead of raising UnicodeEncodeError."""
    try:
        if getattr(stream, "errors", "replace") == "strict":
            stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, io.UnsupportedOperation):
        pass


def _emoji_ok(console: Console) -> bool:
    """Emojis need a UTF-8 output and a terminal font that has them."""
    if os.environ.get("RPGSYNC_ASCII"):
        return False
    encoding = (console.encoding or "").lower().replace("-", "").replace("_", "")
    if not encoding.startswith("utf") or console.legacy_windows:
        return False
    if sys.platform == "win32":
        # Python writes UTF-8 to every Windows console, but the classic console
        # (cmd.exe, PowerShell window) shows emojis as boxes: only trust Windows
        # Terminal and the VS Code terminal
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))
    return True


class UI:
    """rich output on stdout or stderr: colors on a terminal (unless NO_COLOR is set),
    emojis only where the output encoding can show them."""

    def __init__(self, err: bool = False):
        _never_fail(sys.stderr if err else sys.stdout)
        # markup/emoji off: paths and game texts may contain "[...]" or ":name:"
        self.console = Console(stderr=err, highlight=False, soft_wrap=True, markup=False, emoji=False)
        self.fancy = _emoji_ok(self.console)

    def marker(self, kind: str) -> str:
        emoji, ascii_marker = MARKERS[kind]
        return (emoji if self.fancy else ascii_marker) + " "

    def print(self, *parts: str | Text | tuple[str, str], kind: str | None = None) -> None:
        """One line made of strings, Texts or (text, style) pairs, after an optional marker."""
        t = Text(self.marker(kind)) if kind else Text()
        for part in parts:
            if isinstance(part, Text):
                t.append_text(part)
            elif isinstance(part, tuple):
                t.append(*part)
            else:
                t.append(part)
        self.console.print(t)

    def echo(self, line: str) -> None:
        self.console.print(Text(line))

    def report(self, r: Result) -> None:
        """A sync result as a log line: marker, time, unit, message, duration."""
        if r.action == "none" and not r.message:
            return
        kind = {"export": "script", "import": "game", "merge": "merge", "conflict": "conflict", "error": "error"}
        style = {"error": "red", "conflict": "yellow"}.get(r.action, "")
        first, _, rest = (r.message or r.action).partition("\n")
        msg = Text(first, style)
        msg.highlight_regex(FILE_NAMES, "cyan")
        parts: list = [("[%s] " % time.strftime("%H:%M:%S"), "dim"), (r.unit, "bold"), ": ", msg]
        if r.seconds is not None and r.action != "none":
            parts.append((" (%.2f s)" % r.seconds, "dim"))
        if rest:
            parts.append(("\n" + rest, style))
        self.print(*parts, kind=kind.get(r.action, "info"))


class CliSyncer(Syncer):
    """A Syncer reporting through rich."""

    def __init__(self, project: Project):
        self.ui = UI()
        super().__init__(project, log=self.ui.echo)

    def report(self, r: Result) -> None:
        self.ui.report(r)


def _fail(message: str, code: int = 2) -> typer.Exit:
    UI(err=True).print(("Error: %s" % message, "red"), kind="error")
    return typer.Exit(code)


# --------------------------------------------------------------------------
# Finding the project
# --------------------------------------------------------------------------


class ProjectNotFound(Exception):
    pass


class Located(BaseModel):
    game: str
    scripts: str | None = None


def _find(where: Path) -> Located | None:
    """The game folder (RPG_RT.ldb) or scripts folder (pyproject.toml with
    [tool.rpgsync] game = "...") containing `where`; the nearest one wins."""
    for d in (where, *where.parents):
        if (d / "RPG_RT.ldb").is_file():
            return Located(game=str(d))
        pyproject = d / "pyproject.toml"
        if pyproject.is_file():
            try:
                game = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("rpgsync", {})
                game = game.get("game")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(game, str) and (d / game / "RPG_RT.ldb").is_file():
                return Located(game=str((d / game).resolve()), scripts=str(d))
    return None


def _locate(project: str | None) -> Located:
    """The project of a folder (default: the current one)."""
    if project is not None and not os.path.isdir(project):
        raise typer.BadParameter("%s is not a folder" % project)
    where = Path(project if project is not None else os.getcwd()).resolve()
    found = _find(where)
    if found is None:
        raise ProjectNotFound(
            "no RPG Maker game at %s: neither it nor a parent folder holds RPG_RT.ldb or a scripts folder "
            "(pyproject.toml with [tool.rpgsync]).\nRun rpgsync inside your game folder, or pass its path." % where
        )
    return found


def _open(project: str | None) -> CliSyncer:
    try:
        loc = _locate(project)
    except ProjectNotFound as e:
        raise _fail(str(e)) from None
    return CliSyncer(Project(loc.game, loc.scripts))


def _set_up(syncer: CliSyncer) -> None:
    """First run on a game: make its scripts folder a small Python project
    (pyproject.toml, tests, editor settings) and install it with uv."""
    if os.path.exists(os.path.join(syncer.project.script_dir, "pyproject.toml")):
        return
    ui = syncer.ui
    for path in init_scripts_project(syncer.project):
        ui.print("wrote ", (path, "cyan"), kind="file")
    if shutil.which("uv"):
        subprocess.run(["uv", "sync", "--quiet"], cwd=syncer.project.script_dir, check=False)
    ui.print(("Scripts folder ready: ", "bold green"), (syncer.project.script_dir, "cyan"), kind="ok")


def _units(syncer: Syncer, maps: list[int] | None, no_common: bool) -> Iterator[Unit]:
    for u in syncer.units():
        if u.kind == "common" and no_common:
            continue
        if maps and (u.kind != "map" or u.map_id not in maps):
            continue
        yield u


def _run(syncer: CliSyncer, units, prefer: str | None) -> None:
    failed = unchanged = reported = 0
    for u in units:
        start = time.perf_counter()
        r = syncer.sync(u, prefer)
        r.seconds = time.perf_counter() - start
        if r.action in ("export", "import") and r.message.endswith(": no changes"):
            unchanged += 1  # rewritten identically: one summary line for all of them
            continue
        syncer.report(r)
        reported += 1
        failed += r.action in ("error", "conflict")
    if unchanged:
        files = "%d %sfile%s" % (unchanged, "other " if reported else "", "s" * (unchanged > 1))
        syncer.ui.print(("%s already up to date" % files, "dim"))
    if failed:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# Status helpers
# --------------------------------------------------------------------------

Part = tuple[str, str]  # (text, rich style)

PLURAL = {"event": "events", "common event": "common events", "entry": "entries"}


def _ids(ids: list[int], limit: int = 8) -> str:
    """1, 2, 3, 5 -> "1-3, 5"; long lists are cut."""
    runs: list[list[int]] = []
    for i in ids:
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    out = [str(a) if a == b else "%d-%d" % (a, b) for a, b in runs]
    if len(out) > limit:
        out = out[:limit] + ["... (%d in all)" % len(ids)]
    return ", ".join(out)


def _entry_diff(old: list, new: list, what: str) -> list[Part]:
    """Per-entry difference between two spec lists: what changed from old to new."""
    a = {s.id: s for s in old if s.id is not None}
    b = {s.id: s for s in new if s.id is not None}
    parts: list[Part] = []
    if 0 in a and 0 in b and a[0].key() != b[0].key():  # id 0: the table size
        parts.append(("size %d -> %d" % (a[0].size, b[0].size), "yellow"))
    for label, ids, style in (
        ("changed", sorted(i for i in a.keys() & b.keys() if i and a[i].key() != b[i].key()), "yellow"),
        ("added", sorted(i for i in b.keys() - a.keys() if i), "green"),
        ("removed", sorted(i for i in a.keys() - b.keys() if i), "red"),
    ):
        if ids:
            parts.append(("%s %s %s" % (PLURAL[what] if len(ids) > 1 else what, _ids(ids), label), style))
    return parts


def _line_counts(old: str, new: str) -> tuple[int, int]:
    """Lines added and removed from old to new, like `git diff --stat`."""
    plus = minus = 0
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            minus += i2 - i1
            plus += j2 - j1
    return plus, minus


class StatusLine(BaseModel):
    name: str
    counts: list[tuple[str, int, int]] = []  # (label, added lines, removed lines)
    detail: list[Part] = []


def _counts_text(counts: list[tuple[str, int, int]]) -> Text:
    t = Text()
    for n, (label, plus, minus) in enumerate(counts):
        if n:
            t.append(", ")
        if label:
            t.append(label + " ")
        t.append("+%d" % plus, "green")
        t.append(" ")
        t.append("-%d" % minus, "red")
    return t


def _render(ui: UI, kind: str, title: str, lines: list[StatusLine], label: str, style: str, w: int, cw: int) -> None:
    ui.print((title, "bold"), kind=kind)
    console = ui.console
    for line in lines:
        t = Text(" " * 8)
        if label:
            t.append(label.ljust(12), style)
        t.append(line.name.ljust(w), style)
        if line.counts or line.detail:
            counts = _counts_text(line.counts)
            t.append("   ")
            t.append_text(counts)
            t.append(" " * (cw - len(counts.plain)))
        for n, (text, part_style) in enumerate(line.detail):
            t.append("   " if n == 0 else "; ")
            t.append(text, part_style)
        console.print(t)


def _status(syncer: Syncer, units: list[Unit]) -> dict[str, list[StatusLine]]:
    project = syncer.project
    out: dict[str, list[StatusLine]] = {k: [] for k in ("script", "game", "both", "unsynced", "errors", "new")}
    data_of: dict[str, bytes | None] = {}
    for u in units:
        if u.bin_path not in data_of:
            data_of[u.bin_path] = read_bytes(u.bin_path)
        data = data_of[u.bin_path]
        if data is None:
            continue
        what = {"map": "event", "common": "common event"}.get(u.kind, "entry")
        py_name = os.path.relpath(u.py_path, project.script_dir)
        bin_name = os.path.relpath(u.bin_path, project.game_dir)
        if u.kind == "database":
            bin_name += " [%s]" % u.table
        elif u.kind == "common":
            bin_name += " [common events]"
        if not os.path.exists(u.py_path):
            out["new"].append(StatusLine(name=py_name))
            continue
        with open(u.py_path, encoding="utf-8") as f:
            text = f.read()
        st = syncer.state.get(u)
        bin_changed = st is None or u.fingerprint(data) != st["bin"]
        py_changed = st is None or sha(text.encode("utf-8")) != st["py"]
        if not (bin_changed or py_changed):
            continue

        specs: list = []
        if py_changed:
            try:
                specs, _ = u.compile_checked(text, data)
            except ScriptError as e:
                line = ":%d" % e.err.lineno if e.err.lineno else ""
                out["errors"].append(StatusLine(name="%s%s: %s" % (py_name, line, e.err)))
                continue
            problems = syncer.check_engine(u, specs, data)
            if problems:
                out["errors"] += [StatusLine(name="%s: %s" % (py_name, p)) for p in problems]
                continue
        game_specs = u.specs_from_bin(data)

        if st is None:  # never synced: a sync adopts the pair if it agrees
            detail = _entry_diff(game_specs, specs, what)
            if detail:
                out["unsynced"].append(StatusLine(name=py_name, detail=detail))
            continue

        base = syncer.state.base_text(u)
        base_specs = None
        if base is not None and bin_changed:
            try:
                base_specs = u.compile(base)
            except Exception:  # base written by an older rpgsync, renamed switch...
                base_specs = None
        pulled = None
        if bin_changed:
            try:
                pulled = u.decompile(data)
            except Exception:
                pulled = None

        if py_changed and not bin_changed:
            counts = [("", *_line_counts(base, text))] if base is not None else []
            detail = _entry_diff(game_specs, specs, what) or [("no change for the game", "dim")]
            out["script"].append(StatusLine(name=py_name, counts=counts, detail=detail))
        elif bin_changed and not py_changed:
            counts = [("", *_line_counts(text, pulled))] if base is not None and pulled is not None else []
            detail = _entry_diff(base_specs, game_specs, what) if base_specs is not None else []
            out["game"].append(StatusLine(name=bin_name, counts=counts, detail=detail))
        else:
            counts = []
            if base is not None:
                counts.append(("scripts", *_line_counts(base, text)))
                if pulled is not None:
                    counts.append(("game", *_line_counts(base, pulled)))
            detail = []
            if base_specs is not None:
                for side, old, new in (("scripts", base_specs, specs), ("game", base_specs, game_specs)):
                    parts = _entry_diff(old, new, what)
                    if parts:  # "scripts: event 9 changed; game: event 1 changed"
                        detail += [(side + ": " + parts[0][0], parts[0][1])] + parts[1:]
            out["both"].append(StatusLine(name="%s / %s" % (py_name, bin_name), counts=counts, detail=detail))
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def watch(
    project: ProjectArg = None,
    interval: Annotated[float, typer.Option(help="Poll interval in seconds.")] = 0.5,
):
    """Keep the game and its scripts in sync until Ctrl+C (also: `rpgsync [FOLDER]`).
    The first time, the scripts are written and their folder is set up."""
    syncer = _open(project)
    _set_up(syncer)
    ui = syncer.ui
    ui.print(
        "watching ",
        (syncer.project.game_dir, "cyan"),
        " (scripts in ",
        (syncer.project.script_dir, "cyan"),
        ") - Ctrl+C to stop",
        kind="watch",
    )
    try:
        syncer.watch(interval, announce=False)
    except KeyboardInterrupt:
        ui.print("stopped", kind="stop")


def pull(
    project: ProjectArg = None,
    map: MapOpt = None,
    no_common: NoCommonOpt = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite scripts that have unsynced edits.")] = False,
):
    """Game -> scripts: regenerate the scripts from the game files."""
    syncer = _open(project)
    _set_up(syncer)
    _run(syncer, _units(syncer, map, no_common), "game" if force else None)


def push(project: ProjectArg = None, map: MapOpt = None, no_common: NoCommonOpt = False):
    """Scripts -> game: write the scripts into the game files."""
    syncer = _open(project)
    _run(syncer, _units(syncer, map, no_common), "script")


def status(project: ProjectArg = None, map: MapOpt = None, no_common: NoCommonOpt = False):
    """Show what is pending on each side, without writing anything."""
    syncer = _open(project)
    ui = syncer.ui
    engine = ENGINES.get(syncer.project.context().engine, syncer.project.context().engine)
    ui.print("On ", (os.path.basename(syncer.project.game_dir), "bold"), " (%s)" % engine)
    ui.print("  game:    ", (syncer.project.game_dir, "cyan"))
    ui.print("  scripts: ", (syncer.project.script_dir, "cyan"))
    ui.echo("")

    found = _status(syncer, list(_units(syncer, map, no_common)))
    listed = [line for k in ("script", "game", "both", "unsynced") for line in found[k]]
    if not listed and not found["errors"] and not found["new"]:
        ui.print(("All files are synced", "bold green"), kind="ok")
        return
    w = max((len(line.name) for line in listed), default=0)
    cw = max((len(_counts_text(line.counts).plain) for line in listed), default=0)
    sections = (
        ("script", "Changes in scripts (`rpgsync push` writes them into the game):", "modified:", "yellow"),
        ("game", "Changes in the game (`rpgsync pull` updates the scripts):", "modified:", "yellow"),
        ("merge", "Changed on both sides (`rpgsync` merges them per event):", "modified:", "yellow"),
        (
            "conflict",
            "Never synced and different (`rpgsync push` keeps the scripts, `rpgsync pull --force` keeps the game):",
            "differs:",
            "yellow",
        ),
        ("error", "Scripts with errors (fix them before they can be synced):", "", "red"),
        ("new", "Not exported yet (`rpgsync pull` creates them):", "", "green"),
    )
    keys = {"script": "script", "game": "game", "merge": "both", "conflict": "unsynced", "error": "errors"}
    for kind, title, label, style in sections:
        lines = found[keys.get(kind, kind)]
        if lines:
            _render(ui, kind, title, lines, label, style, w if label else 0, cw)


def check(
    project: ProjectArg = None,
    map: MapOpt = None,
    no_common: NoCommonOpt = False,
    patches: Annotated[
        list[str] | None,
        typer.Option("--patch", help="Allowed engine patch: dynrpg, maniac or easyrpg (repeatable)."),
    ] = None,
    no_types: Annotated[bool, typer.Option("--no-types", help="Skip the type check (ty).")] = False,
):
    """Compile every script, check engine compatibility and types (ty), without writing anything."""
    syncer = _open(project)
    ui, err = syncer.ui, UI(err=True)
    engine = target_engine(syncer.project.script_dir, syncer.project.context().engine)
    declared, allow = load_rules(syncer.project.script_dir)
    errors = 0
    for u in _units(syncer, map, no_common):
        if not os.path.exists(u.py_path):
            continue
        with open(u.py_path, encoding="utf-8") as f:
            text = f.read()
        try:
            specs, _ = u.compile_checked(text)
        except ScriptError as e:
            err.print((str(e), "red"), kind="error")
            errors += 1
            continue
        for problem in check_specs(specs, engine, patches or declared, allow):
            err.print((u.py_path, "cyan"), ": ", (problem, "red"), kind="error")
            errors += 1
    if not no_types:
        errors += _check_types(syncer.project.script_dir, ui, err)
    if errors:
        ui.print(("%d problem(s)" % errors, "bold red"), kind="error")
        raise typer.Exit(1)
    what = "compile and fit the engine" + (" and pass the type check" if not no_types else "")
    ui.print(("All scripts %s" % what, "bold green"), kind="ok")


def _ty(script_dir: str) -> str | None:
    """The ty of the scripts folder's environment, else one on the PATH."""
    for rel in (".venv/bin/ty", ".venv/Scripts/ty.exe"):
        path = os.path.join(script_dir, rel)
        if os.path.isfile(path):
            return path
    return shutil.which("ty")


def _check_types(script_dir: str, ui: UI, err: UI) -> int:
    """Run ty on the scripts folder; print its diagnostics.  -> number of problems"""
    ty = _ty(script_dir)
    if ty is None:
        ui.print("type check skipped: ty is not installed (run `uv sync` in %s)" % script_dir, kind="info")
        return 0
    cmd = [ty, "check", "--output-format", "concise"]
    venv = os.path.join(script_dir, ".venv")
    if os.path.isdir(venv):  # the scripts' environment, even when another virtualenv is active
        cmd += ["--python", venv]
    result = subprocess.run(cmd, cwd=script_dir, capture_output=True, text=True, errors="replace", check=False)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip() and not ln.startswith(("Found ", "All checks"))]
    if result.returncode == 0:
        return 0
    for line in lines:
        err.print((line, "red"), kind="error")
    return max(len(lines), 1)


def clean(
    project: ProjectArg = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
):
    """Delete the backups of overwritten files (Scripts/.rpgsync/history)."""
    syncer = _open(project)
    ui = syncer.ui
    history = Path(syncer.history_dir)
    snapshots = sorted(history.iterdir()) if history.is_dir() else []
    if not snapshots:
        ui.print("no backups to delete", kind="info")
        return
    size = sum(f.stat().st_size for f in history.rglob("*") if f.is_file())
    ui.print(
        "%d backup(s), %.1f MB in " % (len(snapshots), max(size / 1e6, 0.1)), (str(history), "cyan"), kind="delete"
    )
    if not yes and typer.prompt('Type "yes" to delete them', default="", show_default=False) != "yes":
        ui.print("nothing deleted", kind="info")
        raise typer.Exit(1)
    shutil.rmtree(history)
    ui.print(("deleted", "green"), kind="ok")


# registered here so that `rpgsync --help` lists them in this order
for _command in (watch, status, check, pull, push, clean):
    app.command()(_command)

COMMANDS = {"watch", "status", "check", "pull", "push", "clean"}


def main() -> None:
    """Entry point.  `rpgsync FOLDER` is a shortcut for `rpgsync watch FOLDER`,
    and a bare `rpgsync` watches the game of the current folder."""
    for stream in (sys.stdout, sys.stderr):
        _never_fail(stream)  # legacy Windows consoles: "?" rather than UnicodeEncodeError
    argv = sys.argv[1:]
    if not argv:
        try:
            _locate(None)
        except ProjectNotFound as e:
            sys.argv.append("--help")
            try:
                app(prog_name="rpgsync")
            except SystemExit:
                pass
            UI(err=True).echo("")
            _fail(str(e))
            sys.exit(2)
        sys.argv.insert(1, "watch")
    elif argv[0] not in COMMANDS and argv[0] != "--help":
        # a project name/folder, or watch options such as --interval
        sys.argv.insert(1, "watch")
    app(prog_name="rpgsync")
