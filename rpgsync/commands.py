"""Event command catalog: binary <-> script call translation.

Every command can always be written in the *generic* form::

    cmd("TintScreen", 100, 100, 100, 100, 20, 1)
    cmd(12345, 1, 2, text="string parameter")

Common commands additionally have a *rich* form with readable arguments,
e.g. ``variable(1, "=", 9999999)``.  The decompiler only emits a rich form
after checking that compiling it back yields the exact same command, so
round trips stay lossless even for data the rich form does not anticipate
(Maniac Patch extensions, odd parameter counts, ...).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .lcf import Command, MoveCommand


class CompileError(Exception):
    def __init__(self, msg: str, node=None):
        self.lineno = getattr(node, "lineno", None)
        super().__init__(msg)


# --------------------------------------------------------------------------
# Command codes (from liblcf enums.csv) and generic names
# --------------------------------------------------------------------------

CODE_NAMES = {
    10: "END",
    1005: "CallCommonEvent",
    1006: "ForceFlee",
    1007: "EnableCombo",
    1008: "ChangeClass",
    1009: "ChangeBattleCommands",
    5001: "OpenLoadMenu",
    5002: "ExitGame",
    5003: "ToggleAtbMode",
    5004: "ToggleFullscreen",
    5005: "OpenVideoOptions",
    10110: "ShowMessage",
    10120: "MessageOptions",
    10130: "ChangeFaceGraphic",
    10140: "ShowChoice",
    10150: "InputNumber",
    10210: "ControlSwitches",
    10220: "ControlVars",
    10230: "TimerOperation",
    10310: "ChangeGold",
    10320: "ChangeItems",
    10330: "ChangePartyMembers",
    10410: "ChangeExp",
    10420: "ChangeLevel",
    10430: "ChangeParameters",
    10440: "ChangeSkills",
    10450: "ChangeEquipment",
    10460: "ChangeHP",
    10470: "ChangeSP",
    10480: "ChangeCondition",
    10490: "FullHeal",
    10500: "SimulatedAttack",
    10610: "ChangeHeroName",
    10620: "ChangeHeroTitle",
    10630: "ChangeSpriteAssociation",
    10640: "ChangeActorFace",
    10650: "ChangeVehicleGraphic",
    10660: "ChangeSystemBGM",
    10670: "ChangeSystemSFX",
    10680: "ChangeSystemGraphics",
    10690: "ChangeScreenTransitions",
    10710: "EnemyEncounter",
    10720: "OpenShop",
    10730: "ShowInn",
    10740: "EnterHeroName",
    10810: "Teleport",
    10820: "MemorizeLocation",
    10830: "RecallToLocation",
    10840: "EnterExitVehicle",
    10850: "SetVehicleLocation",
    10860: "ChangeEventLocation",
    10870: "TradeEventLocations",
    10910: "StoreTerrainID",
    10920: "StoreEventID",
    11010: "EraseScreen",
    11020: "ShowScreen",
    11030: "TintScreen",
    11040: "FlashScreen",
    11050: "ShakeScreen",
    11060: "PanScreen",
    11070: "WeatherEffects",
    11110: "ShowPicture",
    11120: "MovePicture",
    11130: "ErasePicture",
    11210: "ShowBattleAnimation",
    11310: "PlayerVisibility",
    11320: "FlashSprite",
    11330: "MoveEvent",
    11340: "ProceedWithMovement",
    11350: "HaltAllMovement",
    11410: "Wait",
    11510: "PlayBGM",
    11520: "FadeOutBGM",
    11530: "MemorizeBGM",
    11540: "PlayMemorizedBGM",
    11550: "PlaySound",
    11560: "PlayMovie",
    11610: "KeyInputProc",
    11710: "ChangeMapTileset",
    11720: "ChangePBG",
    11740: "ChangeEncounterSteps",
    11750: "TileSubstitution",
    11810: "TeleportTargets",
    11820: "ChangeTeleportAccess",
    11830: "EscapeTarget",
    11840: "ChangeEscapeAccess",
    11910: "OpenSaveMenu",
    11930: "ChangeSaveAccess",
    11950: "OpenMainMenu",
    11960: "ChangeMainMenuAccess",
    12010: "ConditionalBranch",
    12110: "Label",
    12120: "JumpToLabel",
    12210: "Loop",
    12220: "BreakLoop",
    12310: "EndEventProcessing",
    12320: "EraseEvent",
    12330: "CallEvent",
    12410: "Comment",
    12420: "GameOver",
    12510: "ReturntoTitleScreen",
    13110: "ChangeMonsterHP",
    13120: "ChangeMonsterMP",
    13130: "ChangeMonsterCondition",
    13150: "ShowHiddenMonster",
    13210: "ChangeBattleBG",
    13260: "ShowBattleAnimation_B",
    13310: "ConditionalBranch_B",
    13410: "TerminateBattle",
    20110: "ShowMessage_2",
    20140: "ShowChoiceOption",
    20141: "ShowChoiceEnd",
    20710: "VictoryHandler",
    20711: "EscapeHandler",
    20712: "DefeatHandler",
    20713: "EndBattle",
    20720: "Transaction",
    20721: "NoTransaction",
    20722: "EndShop",
    20730: "Stay",
    20731: "NoStay",
    20732: "EndInn",
    22010: "ElseBranch",
    22011: "EndBranch",
    22210: "EndLoop",
    22410: "Comment_2",
    23310: "ElseBranch_B",
    23311: "EndBranch_B",
}
NAME_CODES = {v: k for k, v in CODE_NAMES.items()}

END = 10

# Block structure.  A "closer" command ends a group of blocks; it is implied
# in scripts and re-inserted when the group's last block ends.
#   closer code -> (codes of blocks that belong to the group, codes that may
#                   *continue* an open group)
GROUPS = {
    22011: ({12010, 22010}, {22010}),  # if / else / end
    23311: ({13310, 23310}, {23310}),  # battle if / else / end
    22210: ({12210}, set()),  # loop / end loop
    20141: ({20140}, {20140}),  # choice options / end
    20713: ({20710, 20711, 20712}, {20710, 20711, 20712}),  # battle handlers
    20722: ({20720, 20721}, {20720, 20721}),  # shop handlers
    20732: ({20730, 20731}, {20730, 20731}),  # inn handlers
}
CLOSER_OF = {}
for _closer, (_members, _cont) in GROUPS.items():
    for _m in _members:
        CLOSER_OF[_m] = _closer


# --------------------------------------------------------------------------
# Script values
# --------------------------------------------------------------------------


class Sym(BaseModel):
    """A bare name in a script, e.g. ``move_up``."""

    name: str
    node: Any = None


class Call(BaseModel):
    """A call in a script: ``wait(1)``, or a method call ``this.move(...)``
    (then ``target`` is the evaluated object)."""

    name: str
    args: list[Any]
    kwargs: dict[str, Any]
    target: Any = None
    node: Any = None


class Ctx(BaseModel):
    """Context shared by the encoders/decoders of one file."""

    encoding: str = "cp1252"
    engine: str = "2k3"  # "2k" or "2k3"
    names: dict[str, dict[int, str]] = Field(default_factory=dict)  # db names for comments
    comments: Any = None  # CommentIndex of the script being compiled
    match_header: bool = False  # encoding/decoding the subject of a `match` (battle, shop, inn)
    event_names: dict[int, str] = Field(default_factory=dict)  # map event names for comments
    handles: dict[str, dict[int, str]] = Field(default_factory=dict)  # variables/switches id -> Python name
    choice_sep: str | None = None  # how the editor joined show_choices options (None: engine default)

    def enc(self, s: str, node=None) -> bytes:
        try:
            return s.encode(self.encoding, errors="surrogateescape")
        except UnicodeEncodeError as e:
            raise CompileError(  # noqa: B904 - report the script position, not the codec error
                "text %r cannot be stored in the game encoding %s (%s)" % (s, self.encoding, e.reason), node
            )

    def dec(self, b: bytes) -> str:
        s = b.decode(self.encoding, errors="surrogateescape")
        if s.encode(self.encoding, errors="surrogateescape") == b:
            return s
        # Some code pages (e.g. cp932) map several byte sequences to the same
        # character.  Keep such characters as escaped bytes so they survive.
        import codecs

        decoder = codecs.getincrementaldecoder(self.encoding)(errors="surrogateescape")
        out, start = [], 0
        for i in range(len(b)):
            chars = decoder.decode(b[i : i + 1])
            if chars:
                raw = b[start : i + 1]
                if chars.encode(self.encoding, errors="surrogateescape") == raw:
                    out.append(chars)
                else:
                    out.append(raw.decode("ascii", errors="surrogateescape"))
                start = i + 1
        if start < len(b):
            out.append(b[start:].decode("ascii", errors="surrogateescape"))
        return "".join(out)


# --------------------------------------------------------------------------
# Source rendering helpers
# --------------------------------------------------------------------------


def pystr(s: str) -> str:
    """Render a Python string literal, preferring raw strings for \\ codes."""
    printable = all((c.isprintable() and not 0xDC80 <= ord(c) <= 0xDCFF) for c in s)
    if "\\" in s and printable and '"' not in s and not s.endswith("\\"):
        return 'r"%s"' % s
    out = ['"']
    for c in s:
        o = ord(c)
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif 0xDC80 <= o <= 0xDCFF or not c.isprintable():
            out.append("\\x%02x" % o if o < 0x100 else "\\u%04x" % o)
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def lit(v) -> str:
    if isinstance(v, bool) or v is None:
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return pystr(v)
    if isinstance(v, tuple):
        return "(" + ", ".join(lit(x) for x in v) + ("," if len(v) == 1 else "") + ")"
    if isinstance(v, list):
        return "[" + ", ".join(lit(x) for x in v) + "]"
    if isinstance(v, Raw):
        return v.src
    raise TypeError(v)


class Raw(BaseModel):
    """Pre-rendered source fragment."""

    src: str


def render_call(name: str, args: Sequence = (), kwargs: Sequence[tuple[str, Any]] = ()) -> str:
    parts = [lit(a) for a in args] + ["%s=%s" % (k, lit(v)) for k, v in kwargs]
    return "%s(%s)" % (name, ", ".join(parts))


# --------------------------------------------------------------------------
# Argument helpers for encoders
# --------------------------------------------------------------------------


class Args:
    """Positional/keyword argument access with good error messages."""

    def __init__(self, call: Call, spec: Sequence[str]):
        self.call = call
        self.values: dict[str, Any] = {}
        if len(call.args) > len(spec):
            raise CompileError(
                "%s() takes at most %d positional arguments (%s)" % (call.name, len(spec), ", ".join(spec)), call.node
            )
        for name, v in zip(spec, call.args):
            self.values[name] = v
        for k, v in call.kwargs.items():
            if k not in spec:
                raise CompileError(
                    "%s() got an unknown argument %r (expected: %s)" % (call.name, k, ", ".join(spec)), call.node
                )
            if k in self.values:
                raise CompileError("%s() got %r twice" % (call.name, k), call.node)
            self.values[k] = v

    def get(self, name, default=None):
        return self.values.get(name, default)

    def req(self, name):
        if name not in self.values:
            raise CompileError("%s() is missing the argument %r" % (self.call.name, name), self.call.node)
        return self.values[name]

    def int(self, name, default=None) -> int:
        v = self.values.get(name, default) if default is not None else self.req(name)
        if isinstance(v, bool):
            return int(v)
        if not isinstance(v, int):
            raise CompileError("%s(): %s must be an integer, got %r" % (self.call.name, name, v), self.call.node)
        return v

    def bool(self, name, default=False) -> bool:
        v = self.values.get(name, default)
        if not isinstance(v, (bool, int)):
            raise CompileError("%s(): %s must be True/False" % (self.call.name, name), self.call.node)
        return bool(v)

    def str(self, name, default=None) -> str:
        v = self.values.get(name, default) if default is not None else self.req(name)
        if v is None:
            return ""
        if not isinstance(v, str):
            raise CompileError("%s(): %s must be a string, got %r" % (self.call.name, name, v), self.call.node)
        return v

    def enum(self, name, table: Sequence[str], default=None) -> int:
        v = self.values.get(name, default) if default is not None else self.req(name)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if isinstance(v, str) and v in table:
            return list(table).index(v)
        raise CompileError(
            "%s(): %s must be one of %s, got %r" % (self.call.name, name, ", ".join(repr(t) for t in table if t), v),
            self.call.node,
        )


def enum_name(table: Sequence[str], v: int):
    if 0 <= v < len(table) and table[v]:
        return table[v]
    return v


def is_call(v, *names) -> bool:
    return isinstance(v, Call) and v.name in names


# --------------------------------------------------------------------------
# Shared enums
# --------------------------------------------------------------------------

DIRECTIONS = ["up", "right", "down", "left"]
TELEPORT_DIRECTIONS = ["retain", "up", "right", "down", "left"]

MOVES = [
    "move_up",
    "move_right",
    "move_down",
    "move_left",
    "move_up_right",
    "move_down_right",
    "move_down_left",
    "move_up_left",
    "move_random",
    "move_toward_player",
    "move_away_from_player",
    "move_forward",
    "face_up",
    "face_right",
    "face_down",
    "face_left",
    "turn_right",
    "turn_left",
    "turn_around",
    "turn_random",
    "face_random",
    "face_player",
    "face_away_from_player",
    "pause",
    "jump_start",
    "jump_end",
    "lock_facing",
    "unlock_facing",
    "speed_up",
    "speed_down",
    "frequency_up",
    "frequency_down",
    "switch_on",
    "switch_off",
    "change_graphic",
    "play_se",
    "phasing_on",
    "phasing_off",
    "stop_animation",
    "start_animation",
    "transparency_up",
    "transparency_down",
]


# --------------------------------------------------------------------------
# Move routes
# --------------------------------------------------------------------------


def moves_to_src(moves: list[MoveCommand], ctx: Ctx) -> list[Raw]:
    """Move steps as source; runs of the same simple step become ``step * n``."""
    out: list[Raw] = []
    i = 0
    while i < len(moves):
        m = moves[i]
        j = i + 1
        if m.code not in (32, 33, 34, 35) and 0 <= m.code < len(MOVES):
            while j < len(moves) and moves[j].code == m.code:
                j += 1
            if j - i > 1:
                out.append(Raw(src="%s * %d" % (MOVES[m.code], j - i)))
                i = j
                continue
        out.extend(_move_src(m, ctx))
        i += 1
    return out


def _move_src(m: MoveCommand, ctx: Ctx) -> list[Raw]:
    if m.code in (32, 33):
        return [Raw(src=render_call(MOVES[m.code], [m.params[0]]))]
    if m.code == 34:
        return [Raw(src=render_call("change_graphic", [ctx.dec(m.string), m.params[0]]))]
    if m.code == 35:
        kw = [(k, v) for k, v, d in zip(("volume", "tempo", "balance"), m.params, (100, 100, 50)) if v != d]
        return [Raw(src=render_call("play_se", [ctx.dec(m.string)], kw))]
    if 0 <= m.code < len(MOVES):
        return [Raw(src=MOVES[m.code])]
    return [Raw(src="move(%d)" % m.code)]


def moves_from_values(values: Sequence[Any], ctx: Ctx, node=None) -> list[MoveCommand]:
    out = []
    expanded = []
    for v in values:
        if type(v).__name__ == "Repeat":
            expanded.extend([v.value] * v.count)
        else:
            expanded.append(v)
    for v in expanded:
        if isinstance(v, Sym):
            if v.name not in MOVES or MOVES.index(v.name) in (32, 33, 34, 35):
                raise CompileError("unknown move command %r" % v.name, v.node or node)
            out.append(MoveCommand(code=MOVES.index(v.name)))
        elif isinstance(v, Call):
            if v.name in ("switch_on", "switch_off"):
                a = Args(v, ["switch"])
                out.append(MoveCommand(code=MOVES.index(v.name), params=[a.int("switch")]))
            elif v.name == "change_graphic":
                a = Args(v, ["charset", "index"])
                out.append(MoveCommand(code=34, string=ctx.enc(a.str("charset"), v.node), params=[a.int("index")]))
            elif v.name == "play_se":
                a = Args(v, ["name", "volume", "tempo", "balance"])
                out.append(
                    MoveCommand(
                        code=35,
                        string=ctx.enc(a.str("name"), v.node),
                        params=[a.int("volume", 100), a.int("tempo", 100), a.int("balance", 50)],
                    )
                )
            elif v.name == "move":
                a = Args(v, ["code"])
                out.append(MoveCommand(code=a.int("code")))
            elif v.name in MOVES:
                out.append(MoveCommand(code=MOVES.index(v.name)))
            else:
                raise CompileError("unknown move command %s()" % v.name, v.node or node)
        else:
            raise CompileError("expected a move command, got %r" % (v,), node)
    return out


# --------------------------------------------------------------------------
# Rich command registry
# --------------------------------------------------------------------------

Decoder = Callable[[Command, Ctx], str | None]
Encoder = Callable[[Call, Ctx], Command]

RICH_DECODERS: dict[int, Decoder] = {}
RICH_ENCODERS: dict[str, Encoder] = {}


def rich(code: int, *names: str):
    def deco(pair):
        dec, enc = pair
        if dec is not None:
            RICH_DECODERS[code] = dec
        for n in names:
            RICH_ENCODERS[n] = enc
        return pair

    return deco


def C(code, params=(), string=b"") -> Command:
    return Command(code=code, string=string, params=list(params))


# -- Messages -----------------------------------------------------------------
# text()/comment() span several commands and are handled by the script layer;
# see TEXT_CODES.
TEXT_CODES = {10110: ("text", 20110), 12410: ("comment", 22410)}


def _msg_options():
    def dec(c, ctx):
        if len(c.params) != 4 or c.string:
            return None
        kw = []
        if c.params[0]:
            kw.append(("transparent", bool(c.params[0]) if c.params[0] == 1 else c.params[0]))
        kw.append(("position", enum_name(["top", "middle", "bottom"], c.params[1])))
        if c.params[2]:
            kw.append(("avoid_hero", bool(c.params[2]) if c.params[2] == 1 else c.params[2]))
        if c.params[3]:
            kw.append(("continue_events", bool(c.params[3]) if c.params[3] == 1 else c.params[3]))
        return render_call("message_options", [], kw)

    def enc(call, ctx):
        a = Args(call, ["transparent", "position", "avoid_hero", "continue_events"])
        return C(
            10120,
            [
                a.int("transparent", 0),
                a.enum("position", ["top", "middle", "bottom"], "bottom"),
                a.int("avoid_hero", 0),
                a.int("continue_events", 0),
            ],
        )

    return dec, enc


rich(10120, "message_options")(_msg_options())


def _face():
    def dec(c, ctx):
        if not c.string:
            if not c.params:
                return "face(None)"
            if len(c.params) == 3:
                flags = [bool(x) if x in (0, 1) else x for x in c.params[1:]]
                return render_call("face", [None, c.params[0]] + flags)
            return None
        if len(c.params) != 3:
            return None
        kw = []
        if c.params[1]:
            kw.append(("right", bool(c.params[1]) if c.params[1] == 1 else c.params[1]))
        if c.params[2]:
            kw.append(("flip", bool(c.params[2]) if c.params[2] == 1 else c.params[2]))
        return render_call("face", [ctx.dec(c.string), c.params[0]], kw)

    def enc(call, ctx):
        a = Args(call, ["faceset", "index", "right", "flip"])
        if a.get("faceset") is None and len(a.values) <= 1:
            return C(10130)
        return C(10130, [a.int("index", 0), a.int("right", 0), a.int("flip", 0)], ctx.enc(a.str("faceset"), call.node))

    return dec, enc


rich(10130, "face")(_face())


# -- Map / movement --------------------------------------------------------------


def _teleport():
    def dec(c, ctx):
        n = len(c.params)
        if n not in (3, 4):
            return None
        if (n == 3) != (ctx.engine == "2k"):
            return None
        kw = []
        if n == 4 and c.params[3]:
            kw.append(("direction", enum_name(TELEPORT_DIRECTIONS, c.params[3])))
        return render_call("teleport", c.params[:3], kw)

    def enc(call, ctx):
        a = Args(call, ["map", "x", "y", "direction"])
        p = [a.int("map"), a.int("x"), a.int("y")]
        if ctx.engine != "2k" or "direction" in a.values:
            p.append(a.enum("direction", TELEPORT_DIRECTIONS, "retain"))
        return C(10810, p)

    return dec, enc


rich(10810, "teleport")(_teleport())


# -- Flow ---------------------------------------------------------------------------


def _wait():
    def dec(c, ctx):
        n = len(c.params)
        if n not in (1, 2) or (n == 1) != (ctx.engine == "2k"):
            return None
        kw = []
        if n == 2 and c.params[1]:
            if c.params[1] != 1:
                return None
            kw.append(("key", True))
        t = c.params[0] / 10
        return render_call("wait", [Raw(src=repr(t))], kw)

    def enc(call, ctx):
        a = Args(call, ["seconds", "key"])
        s = a.get("seconds", 0)
        if not isinstance(s, (int, float)) or isinstance(s, bool):
            raise CompileError("wait(): seconds must be a number", call.node)
        tenths = round(s * 10)
        if abs(tenths - s * 10) > 1e-6:
            raise CompileError("wait(): RPG Maker waits in steps of 0.1 seconds", call.node)
        p = [int(tenths)]
        if ctx.engine != "2k" or a.bool("key"):
            p.append(int(a.bool("key")))
        return C(11410, p)

    return dec, enc


rich(11410, "wait")(_wait())


def _simple(code, name, argnames=()):
    def dec(c, ctx):
        if len(c.params) != len(argnames) or c.string:
            return None
        return render_call(name, c.params)

    def enc(call, ctx):
        a = Args(call, list(argnames))
        return C(code, [a.int(n) for n in argnames])

    return dec, enc


for _code, _name, _args in [
    (12110, "label", ("id",)),
    (12120, "goto", ("id",)),
    (12420, "game_over", ()),
    (12510, "return_to_title", ()),
    (11340, "wait_for_movement", ()),
    (11350, "halt_all_movement", ()),
    (11530, "memorize_bgm", ()),
    (11540, "play_memorized_bgm", ()),
    (11910, "open_save_menu", ()),
    (11950, "open_main_menu", ()),
]:
    rich(_code, _name)(_simple(_code, _name, _args))


def _sound(code, name, has_fade):
    keys = (["fade"] if has_fade else []) + ["volume", "tempo", "balance"]
    defaults = ([0] if has_fade else []) + [100, 100, 50]

    def dec(c, ctx):
        if len(c.params) != len(keys):
            return None
        kw = [(k, v) for k, v, d in zip(keys, c.params, defaults) if v != d]
        return render_call(name, [ctx.dec(c.string)], kw)

    def enc(call, ctx):
        a = Args(call, ["name"] + keys)
        return C(code, [a.int(k, d) for k, d in zip(keys, defaults)], ctx.enc(a.str("name", ""), call.node))

    return dec, enc


rich(11510, "play_bgm")(_sound(11510, "play_bgm", True))
rich(11550, "play_se")(_sound(11550, "play_se", False))


# --------------------------------------------------------------------------
# Generic form
# --------------------------------------------------------------------------


def generic_src(c: Command, ctx: Ctx, with_indent: bool = False) -> str:
    head = CODE_NAMES.get(c.code, c.code)
    kw = []
    if c.string:
        kw.append(("text", ctx.dec(c.string)))
    if with_indent:
        kw.append(("indent", c.indent))
    return render_call("cmd", [head] + list(c.params), kw)


def generic_encode(call: Call, ctx: Ctx) -> tuple[Command, int | None]:
    """-> (command, explicit indent or None)"""
    if not call.args:
        raise CompileError("cmd() needs a command code or name", call.node)
    head = call.args[0]
    if isinstance(head, str):
        if head not in NAME_CODES:
            raise CompileError("unknown command name %r" % head, call.node)
        code = NAME_CODES[head]
    elif isinstance(head, int) and not isinstance(head, bool):
        code = head
    else:
        raise CompileError("cmd(): first argument must be a command code or name", call.node)
    params = call.args[1:]
    for p in params:
        if not isinstance(p, int) or isinstance(p, bool):
            raise CompileError("cmd(): parameters must be integers, got %r" % (p,), call.node)
    for k in call.kwargs:
        if k not in ("text", "indent"):
            raise CompileError("cmd() got an unknown argument %r" % k, call.node)
    text = call.kwargs.get("text", "")
    if not isinstance(text, str):
        raise CompileError("cmd(): text must be a string", call.node)
    indent = call.kwargs.get("indent")
    return Command(code=code, string=ctx.enc(text, call.node), params=list(params)), indent


def decode_command(c: Command, ctx: Ctx) -> str:
    """Best script form for a single command (verified by caller)."""
    dec = RICH_DECODERS.get(c.code)
    if dec is not None:
        try:
            src = dec(c, ctx)
        except (IndexError, ValueError):
            src = None
        if src is not None:
            return src
    return generic_src(c, ctx)


def encode_call(call: Call, ctx: Ctx) -> tuple[Command, int | None]:
    if call.name == "cmd":
        return generic_encode(call, ctx)
    enc = RICH_ENCODERS.get(call.name)
    if enc is None:
        raise CompileError("unknown command %s()" % call.name, call.node)
    return enc(call, ctx), None


# --------------------------------------------------------------------------
# Comments with database names (purely informative)
# --------------------------------------------------------------------------


def hint(c: Command, ctx: Ctx) -> str | None:
    def nm(kind, i):
        if i in ctx.handles.get(kind, {}):
            return None  # written by name in the code already
        n = ctx.names.get(kind, {}).get(i)
        return "".join(ch for ch in n if ch.isprintable()) if n else None

    p = c.params
    try:
        if c.code == 10220 and p[0] == 0:
            return nm("variables", p[1])
        if c.code == 10210 and p[0] == 0:
            return nm("switches", p[1])
        if c.code == 12010 and p[0] == 0:
            return nm("switches", p[1])
        if c.code == 12010 and p[0] == 1:
            return nm("variables", p[1])
        if c.code == 10320 and p[1] == 0:
            return nm("items", p[2])
        if c.code == 10330 and p[1] == 0:
            return nm("actors", p[2])
        if c.code == 12330 and p[0] == 0:
            return nm("commonevents", p[1])
    except IndexError:
        return None
    return None


# --------------------------------------------------------------------------
# `size = N` in database files
# --------------------------------------------------------------------------


class TableSize(BaseModel):
    """``size = N`` at the top of a database file: how many slots the table
    has in the editor.  Travels with the entries as a pseudo-entry of id 0,
    so that it is merged like them."""

    id: int = 0
    size: int
    lineno: int | None = None

    def key(self) -> tuple:
        return ("size", self.size)


SIZE_NOTE = "slots in the editor; lower it to drop empty slots at the end"


def config_src(size: int, indent: str = "    ") -> list[str]:
    """The ``class Config:`` block of a database class."""
    return [indent + "class Config:", indent + "    size = %d  # %s" % (size, SIZE_NOTE)]


def size_stmt(stmt: ast.stmt) -> TableSize | None:
    """The TableSize of a ``class Config: size = N`` block (or of a plain
    ``size = N``, as older rpgsync versions wrote it), None for other statements."""
    if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
        found = None
        for sub in stmt.body:
            if isinstance(sub, ast.Pass) or (
                isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str)
            ):
                continue
            size = size_stmt(sub) if not isinstance(sub, ast.ClassDef) else None
            if size is None:
                raise CompileError("class Config may only contain `size = N`", sub)
            if found is not None:
                raise CompileError("size is assigned twice", sub)
            found = size
        if found is None:
            raise CompileError("class Config needs `size = N` (the number of slots)", stmt)
        return found
    if not (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and stmt.targets[0].id == "size"
    ):
        return None
    v = stmt.value
    if not (
        isinstance(v, ast.Constant) and isinstance(v.value, int) and not isinstance(v.value, bool) and v.value >= 0
    ):
        raise CompileError("size must be a number of slots, e.g. size = 100", stmt)
    return TableSize(size=v.value, lineno=stmt.lineno)


def split_size(specs: list) -> tuple[list, int | None]:
    """(entries, size) from a spec list that may hold a TableSize."""
    sizes = [s.size for s in specs if isinstance(s, TableSize)]
    return [s for s in specs if not isinstance(s, TableSize)], (sizes[0] if sizes else None)


def table_length(current: int, size: int | None, ids: list[int]) -> int:
    """Number of slots after an update.  Without `size` the table keeps its
    length; ids past the end grow it.  With `size`, lowering it cuts the
    slots at the end (refused while entries still use them)."""
    top = max(ids, default=0)
    if size is None:
        return max(current, top)
    blocked = sorted(i for i in ids if size < i <= current)
    if blocked:
        shown = (
            ", ".join(map(str, blocked))
            if len(blocked) <= 5
            else "%d of them, %d to %d" % (len(blocked), blocked[0], blocked[-1])
        )
        raise ValueError(
            "size = %d would delete entries still listed (%s): remove them first or raise size" % (size, shown)
        )
    return max(size, top)
