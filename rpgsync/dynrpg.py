"""DynRPG plugins rpgsync knows: their DLL, the commands they add, the DynRPG.ini
settings they read.

DynRPG (https://rewking.com/dynrpg/) loads every DLL of the game's DynPlugins
folder.  Events call a plugin through comments that start with ``@``, which the
scripts write ``dyn.name(...)``::

    @write_text "id", 10, 20, "Hello \\v[3]"    dyn.write_text("id", 10, 20, r"Hello \\v[3]")
    @remove_text "id", end                       dyn.remove_text("id", word.end)

A plugin reads its settings from the game's DynRPG.ini, in its own section (named
after its DLL unless it says otherwise).  With this table, `rpgsync check` warns
about arguments a command doesn't take, calls no installed plugin runs, sections
nothing reads and missing settings.  rpgsync.dsl.DynRPG has the same commands as
typed methods, for editors and ty (tests/test_dynrpg_plugins.py keeps them alike).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from .dynparams import Problem
from .lcf import Command

NUMBER, STRING, WORD, EMPTY = "number", "string", "word", "empty"  # EMPTY: nothing between two commas
KIND_NAMES = {NUMBER: "a number", STRING: 'a "string"', WORD: "a word"}


class Param(BaseModel):
    """One argument of a plugin command."""

    name: str
    kinds: tuple[str, ...]  # what the plugin reads there: number (12, V12), string ("x", N3), word (end)
    optional: bool = False


class Function(BaseModel):
    """A command a plugin adds: ``@name arg, arg``."""

    name: str
    params: list[Param] = []
    more: tuple[str, ...] = ()  # kinds of the arguments after `params`, read too (empty: ignored)
    raw: bool = False  # reads the comment's text after its name as is, commas included


class Plugin(BaseModel):
    dll: str | None  # its file in DynPlugins; None: EasyRPG Player's own commands
    functions: list[Function] = []
    prefix: str | None = None  # every command starting with it is the plugin's too
    section: str | None = None  # its DynRPG.ini section
    keys: list[str] = []  # the settings it reads there (Image<n>: Image1, Image2...)
    required: list[str] = []  # the settings it can't do without
    variables: list[str] = []  # the settings that hold a variable id
    texts: bool = False  # its strings take DynText codes: \v[n], \n[n], \i[n], \t[n]...
    emulated: bool = False  # EasyRPG Player runs its commands without the DLL


def _fn(name: str, *params: str, more: str = "", raw: bool = False) -> Function:
    """``_fn("write_text", "id: string", "fixed?: word|number")``: ``?`` marks an optional
    argument, ``any`` is a number, a string or a word."""
    out = []
    for p in params:
        pname, kinds = p.split(": ")
        out.append(Param(name=pname.rstrip("?"), kinds=_kinds(kinds), optional=pname.endswith("?")))
    return Function(name=name, params=out, more=_kinds(more) if more else (), raw=raw)


def _kinds(text: str) -> tuple[str, ...]:
    return (NUMBER, STRING, WORD) if text == "any" else tuple(text.split("|"))


_CLOCK_VARIABLES = [
    "SessionStart",
    "SessionCurrent",
    "SessionSecs",
    "PlaytimeSecs",
    "PlaytimeHH",
    "PlaytimeMM",
    "PlaytimeSS",
    "RestartTrigger",
    "ForegroundVar",
    "BackgroundVar",
]
_EXP_CURVES = ["CoefficientA", "CoefficientB", "CoefficientC", "CoefficientD", "CoefficientE"]

PLUGINS = [
    # Kazesui's texts drawn on screen; EasyRPG Player: src/dynrpg_textplugin.cpp
    Plugin(
        dll="DynTextPlugin.dll",
        functions=[
            # the text may be a number (V12); fixed: the word fixed scrolls the text with the map
            _fn(
                "write_text",
                "id: string",
                "x: number",
                "y: number",
                "text: string|number",
                "fixed?: any",
                "color?: number",
                "picture?: number",
            ),
            # end: anything (its readme: "just write anything here")
            _fn("append_line", "id: string", "text: string|number", "end?: any"),
            _fn("append_text", "id: string", "text: string|number", "end?: any"),
            _fn("change_text", "id: string", "text: string|number", "color: number|word"),  # end: same color
            _fn("change_position", "id: string", "x: number", "y: number"),
            _fn("remove_text", "id: string", "end?: any"),
            _fn("remove_all"),
        ],
        section="DynTextPlugin",
        keys=["PictureID"],
        texts=True,
        emulated=True,
    ),
    # rewrites the parameters of the next command (rpgsync/dynparams.py checks what they
    # do); EasyRPG Player: src/dynrpg_params.cpp
    Plugin(
        dll="DynParams.dll",
        functions=[
            _fn("dynparams_add_param", "index: number", "value?: number"),  # one number: the value, at the next index
            _fn("dynparams_set_index", "index: number"),
            _fn("dynparams_append_string", "text: word|string", raw=True),
            _fn("dynparams_append_number", "value: number"),
            _fn("dynparams_message_line_append_string", "line: number", "text: string"),
            _fn("dynparams_message_line_append_number", "line: number", "value: number"),
            _fn("dynparams_choice_case_append_string", "choice: number", "text: string"),
            _fn("dynparams_choice_case_append_number", "choice: number", "value: number"),
            _fn("dynparams_overwrite_next"),
            _fn("dynparams_clear_params"),
            _fn("dynparams_list_params"),
            _fn("dynparams_start_record"),
            _fn("dynparams_stop_record"),
        ],
        prefix="dynparams_",  # an unknown one: dynparams.py warns
        section="DynParams",
        keys=["DebugLog"],
        emulated=True,
    ),
    # replaces \V[n] \I[n] \K[n]... (one level nested: \I[\V[n]]) in the Show Message right
    # after the comment, no settings; EasyRPG Player: src/dynrpg_msgcommands.cpp (with the DLL)
    Plugin(dll="msg_commands.dll", functions=[_fn("process_cmd")]),
    # EasyRPG Player's own commands (src/dynrpg_easyrpg.cpp), no DLL
    Plugin(
        dll=None,
        functions=[
            _fn("call", "function: word|string", more="any"),  # runs that command with the other arguments
            _fn("easyrpg_output", "mode: word|string", "message: string", more="any"),  # $1-$9: the ones after
            _fn("easyrpg_add", "variable: number", "value: number", more="number"),  # variable := the sum
        ],
        prefix="easyrpg_",
    ),
    # plugins without commands: they hook the engine
    # PepsiOtaku's battle speed set by a variable (DefaultSpeed: percent, 0 pauses battles)
    Plugin(
        dll="faster_atb_advanced.dll",
        section="faster_atb_advanced",
        keys=["BattleSpeedVariable", "DefaultSpeed"],
        required=["BattleSpeedVariable", "DefaultSpeed"],
        variables=["BattleSpeedVariable"],
    ),
    # PepsiOtaku's play time, shown in the main menu (an image setting left out: no image loads;
    # version 4.1 doesn't read DummyImage)
    Plugin(
        dll="game_clock.dll",
        section="game_clock",
        keys=[
            *_CLOCK_VARIABLES,
            "ForegroundImage",
            "ForegroundImage<n>",
            "DummyImage",
            "BackgroundImage",
            "BackgroundImage<n>",
            "TimeTextDisplay",
        ],
        required=[*_CLOCK_VARIABLES, "ForegroundImage", "BackgroundImage"],
        variables=_CLOCK_VARIABLES,
    ),
    # the EXP curve of every actor; a missing coefficient reads 0 (all 0: a linear curve)
    Plugin(dll="ExpBug-in.dll", section="Exp_Curves", keys=_EXP_CURVES, required=_EXP_CURVES),
    Plugin(dll="ATBWaitOnly.dll"),  # Wait ATB mode on new game and on load, no settings
]

BY_DLL = {p.dll.lower(): p for p in PLUGINS if p.dll}
BY_FUNCTION = {f.name: (p, f) for p in PLUGINS for f in p.functions}
EASYRPG = next(p for p in PLUGINS if p.dll is None)
CORE_SECTIONS = {"quickpatches"}  # read by DynRPG itself


def find(name: str) -> tuple[Plugin | None, Function | None]:
    """The plugin of a command (lowercase name) and its signature, when known."""
    if name in BY_FUNCTION:
        return BY_FUNCTION[name]
    plugin = next((p for p in PLUGINS if p.prefix and name.startswith(p.prefix)), None)
    return plugin, None


# --------------------------------------------------------------------------
# Reading calls as DynRPG does (EasyRPG Player: DynRpg::ParseCommand)
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
_TOKEN = re.compile(r"(n?)(v*)(\d.*)?", re.I)  # N3: actor 3's name, V12: variable 12, NV12, VV12...


class Arg(BaseModel):
    """One argument of a call, as the plugin gets it."""

    kind: str  # NUMBER, STRING, WORD or EMPTY
    text: str  # a string's content; else the token, without its spaces
    quoted: bool = False


def kind(token: str) -> str:
    """What DynRPG makes of an unquoted argument (spaces removed): a number (12, V12,
    VV12), a string (N3, NV12: an actor's name), a word, or empty."""
    if not token:
        return EMPTY
    m = _TOKEN.fullmatch(token)
    if m and (m.group(1) or m.group(2)):
        return STRING if m.group(1) else NUMBER
    return NUMBER if _NUMBER.fullmatch(token) else WORD


def parse(text: str) -> tuple[str, list[Arg]] | None:
    """(function, arguments) of a DynRPG comment; None when it is no call, or one
    DynRPG can't read (something after a string, before the next comma)."""
    m = re.match(r"@([^ ,]*)", text)
    if not m or not m.group(1):
        return None
    name, rest = m.group(1).lower(), text[m.end() :]
    args: list[Arg] = []
    token, mode = "", "arg"  # arg: waiting for one, token, string, comma: after a string
    if rest.startswith(","):
        args.append(Arg(kind=EMPTY, text=""))  # "@f,1": an empty first argument
    i = 1 if rest[:1] in (" ", ",") else 0
    while i < len(rest):
        ch = rest[i]
        if ch in "\r\n":
            pass  # DynRPG skips the line breaks between comment lines, even in strings
        elif mode == "string":
            if ch == '"' and rest[i + 1 : i + 2] == '"':
                token += '"'
                i += 1
            elif ch == '"':
                args.append(Arg(kind=STRING, text=token, quoted=True))
                token, mode = "", "comma"
            else:
                token += ch
        elif ch == ",":
            if mode != "comma":
                args.append(Arg(kind=kind(token), text=token))
            token, mode = "", "arg"
        elif ch == " ":
            pass  # tokens lose their spaces
        elif mode == "comma":
            return None
        elif mode == "arg" and ch == '"':
            mode = "string"
        else:
            token, mode = token + ch, "token"
        i += 1
    if mode == "string":
        args.append(Arg(kind=STRING, text=token, quoted=True))  # not closed: read as if it were
    elif mode == "token" or (mode == "arg" and args):
        args.append(Arg(kind=kind(token), text=token))
    return name, args


def _shown(a: Arg) -> str:
    if a.quoted:
        return '"%s"' % a.text
    if a.kind == WORD:
        return "the word %s" % a.text
    if a.kind == STRING:
        return "%s (an actor's name)" % a.text
    return a.text


# --------------------------------------------------------------------------
# What the game has: DynPlugins/*.dll, DynRPG.ini
# --------------------------------------------------------------------------


class Installed(BaseModel):
    """A game's DynRPG setup: the plugins of its DynPlugins folder and its DynRPG.ini."""

    dlls: list[str] = []  # file names
    ini: dict[str, dict[str, str]] | None = None  # section -> setting -> value; None: no DynRPG.ini

    def has(self, dll: str) -> bool:
        return dll.lower() in {d.lower() for d in self.dlls}

    def unknown(self) -> list[str]:
        """The DLLs this module doesn't know (they may add any command, read any section)."""
        return [d for d in self.dlls if d.lower() not in BY_DLL]


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def installed(game_dir: str) -> Installed:
    """The DynRPG plugins and DynRPG.ini of a game folder (file names in any case)."""
    entries = _listdir(game_dir)
    folder = next((e for e in entries if e.lower() == "dynplugins"), None)
    dlls = [e for e in _listdir(os.path.join(game_dir, folder)) if e.lower().endswith(".dll")] if folder else []
    ini = None
    name = next((e for e in entries if e.lower() == "dynrpg.ini"), None)
    if name is not None:
        try:
            with open(os.path.join(game_dir, name), "rb") as f:
                ini = read_ini(f.read().decode("latin-1"))
        except OSError:
            pass
    return Installed(dlls=sorted(dlls, key=str.lower), ini=ini)


def read_ini(text: str) -> dict[str, dict[str, str]]:
    """Sections of an .ini file and their settings, as Windows reads them: ``;`` starts a
    comment line, the first of a repeated setting wins, lines without ``=`` are ignored."""
    out: dict[str, dict[str, str]] = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and "]" in line:
            name = line[1 : line.index("]")].strip()
            section = out.setdefault(next((s for s in out if s.lower() == name.lower()), name), {})
        elif section is not None and "=" in line:
            key, _, value = line.partition("=")
            section.setdefault(key.strip(), value.strip())
    return out


# --------------------------------------------------------------------------
# Checks, all warnings: a wrong call does nothing (or the wrong thing), RPG_RT goes on
# --------------------------------------------------------------------------

# DynText codes naming a database entry: \v[12], \n[3], \i[5] / \I[5], \t[2] / \T[2]
_CODE = re.compile(r"\\\\|\\([vVnNiItT])\[(\d+)\]")
TABLES = {"v": ("variables", "variable"), "n": ("actors", "actor"), "i": ("items", "item"), "t": ("skills", "skill")}


def _call_text(cmds: Sequence[Command], i: int) -> str:
    text = cmds[i].string
    for c in cmds[i + 1 :]:  # DynRPG reads the comment's other lines too
        if c.code != 22410:
            break
        text += c.string
    return text.decode("latin-1")


def problems(
    cmds: Sequence[Command],
    patches: Iterable[str] = (),
    plugins: Installed | None = None,
    tables: dict[str, int] | None = None,
) -> list[Problem]:
    """Mistakes in the plugin calls of a command list: arguments a known command doesn't
    take, ids that `tables` (the sizes of variables, actors, items, skills) lacks, and,
    when the game's plugins are known, calls no installed plugin runs."""
    patches = set(patches)
    if "dynrpg" in patches:
        runs = b"@"
    elif "easyrpg" in patches:
        runs = b"@easyrpg_"  # EasyRPG Player without DynRPG runs these only
    else:
        return []  # plain comments (compat warns about them)
    out: dict[str, Problem] = {}
    for i, c in enumerate(cmds):
        if c.code != 12410 or not c.string.startswith(runs):
            continue
        call = parse(_call_text(cmds, i))
        if call is None:
            continue
        for message in call_problems(*call, patches=patches, plugins=plugins, tables=tables):
            out.setdefault(message, Problem(error=False, message=message))
    return list(out.values())


def _count(n: int) -> str:
    return "no argument" if n == 0 else "1 argument" if n == 1 else "%d arguments" % n


def call_problems(
    name: str,
    args: list[Arg],
    patches: Iterable[str] = (),
    plugins: Installed | None = None,
    tables: dict[str, int] | None = None,
) -> list[str]:
    """Problems of one call (see problems)."""
    out = []
    call = "dyn.%s()" % name
    plugin, fn = find(name)
    if plugins is not None:
        missing = _not_run(name, plugin, set(patches), plugins)
        if missing:
            out.append(call + missing)
    if fn is not None and not fn.raw:
        required = [p.name for p in fn.params if not p.optional]
        if len(args) < len(required):
            least = "at least " if len(required) < len(fn.params) or fn.more else ""
            out.append(
                "%s needs %s%s (%s), got %d" % (call, least, _count(len(required)), ", ".join(required), len(args))
            )
        for n, a in enumerate(args, 1):
            kinds = fn.params[n - 1].kinds if n <= len(fn.params) else fn.more
            what = "%s (argument %d)" % (fn.params[n - 1].name, n) if n <= len(fn.params) else "argument %d" % n
            if kinds and a.kind != EMPTY and a.kind not in kinds:
                out.append("%s: %s is %s, not %s" % (call, what, " or ".join(KIND_NAMES[k] for k in kinds), _shown(a)))
        extra = len(args) - len(fn.params)
        if extra > 0 and not fn.more:
            names = " (%s)" % ", ".join(p.name for p in fn.params) if fn.params else ""
            ignored = "1 more is" if extra == 1 else "%d more are" % extra
            out.append("%s reads %s%s: %s ignored" % (call, _count(len(fn.params)), names, ignored))
    if tables:
        out += ["%s: %s" % (call, p) for p in _id_problems(args, bool(plugin and plugin.texts), tables)]
    return out


def _not_run(name: str, plugin: Plugin | None, patches: set[str], plugins: Installed) -> str | None:
    """Why no installed plugin runs a command (to follow ``dyn.name()``), None if one may."""
    if plugin is EASYRPG:
        if "easyrpg" in patches:
            return None
        return (
            ' is an EasyRPG Player command, RPG_RT ignores it: add "easyrpg" to [tool.rpgsync] patches '
            "if the game is made for EasyRPG Player"
        )
    if plugin is not None and plugin.dll:
        if plugins.has(plugin.dll) or (plugin.emulated and "easyrpg" in patches):
            return None
        emulated = " (EasyRPG Player emulates it)" if plugin.emulated else ""
        return " comes from %s, which isn't in DynPlugins%s" % (plugin.dll, emulated)
    if plugin is not None or plugins.unknown():
        return None  # an unknown DLL may run it
    return ": no plugin in DynPlugins provides it"


def _id_problems(args: list[Arg], texts: bool, tables: dict[str, int]) -> list[str]:
    """Database entries an argument names that the game lacks: a token's variable or actor
    (V12, N3), and, in DynText strings (`texts`), the ids of its codes (\\v[12], \\i[5]...)."""
    found: list[tuple[str, str, int]] = []  # (as written, table code, id)
    for a in args:
        if a.quoted and texts:
            found += [(m.group(0), m.group(1).lower(), int(m.group(2))) for m in _CODE.finditer(a.text) if m.group(1)]
        elif not a.quoted and a.kind in (NUMBER, STRING) and (m := _TOKEN.fullmatch(a.text)) and m.group(3):
            if (m.group(1) or m.group(2)) and m.group(3).isdigit():
                found.append((a.text, "v" if m.group(2) else "n", int(m.group(3))))
    out = []
    for shown, code, n in found:
        table, what = TABLES[code]
        size = tables.get(table)
        if size is not None and not 1 <= n <= size:
            out.append("%s: the database has no %s %d (it has %d)" % (shown, what, n, size))
    return out


def setup_problems(plugins: Installed, tables: dict[str, int] | None = None) -> list[str]:
    """What is wrong with the game's DynRPG.ini: sections no installed plugin reads,
    settings an installed plugin needs, variables past the database's."""
    out = []
    ini = plugins.ini or {}
    by_lower = {s.lower(): s for s in ini}
    known = [BY_DLL[d.lower()] for d in plugins.dlls if d.lower() in BY_DLL]
    # DynRPG gives a plugin the section named after its DLL; a known one may read another
    readers = CORE_SECTIONS | {os.path.splitext(d)[0].lower() for d in plugins.dlls}
    readers |= {p.section.lower() for p in known if p.section}
    for low, section in by_lower.items():
        if low in readers:
            continue
        owner = next((p for p in PLUGINS if p.section and p.section.lower() == low), None)
        if owner is not None:
            out.append("[%s] is read by %s, which isn't in DynPlugins" % (section, owner.dll))
        elif not plugins.unknown():
            out.append("[%s]: no plugin in DynPlugins reads this section" % section)
    variables = (tables or {}).get("variables")
    for p in known:
        if p.section is None:
            continue
        settings = ini.get(by_lower.get(p.section.lower(), ""))
        if settings is None:
            if p.required:
                out.append("no [%s] section: %s needs %s" % (p.section, p.dll, ", ".join(p.required)))
            continue
        missing = [k for k in p.required if k not in settings]
        if missing:
            out.append("[%s] lacks %s: %s needs %s" % (p.section, ", ".join(missing), p.dll, _them(missing)))
        past = [
            "%s=%s" % (k, settings[k])
            for k in p.variables
            if variables is not None and settings.get(k, "").isdigit() and int(settings[k]) > variables
        ]
        if past:
            what = "a variable" if len(past) == 1 else "variables"
            out.append(
                "[%s] names %s past the database's %d (the editor doesn't show %s): %s"
                % (p.section, what, variables, _them(past), ", ".join(past))
            )
    return out


def _them(items: list) -> str:
    return "it" if len(items) == 1 else "them"
