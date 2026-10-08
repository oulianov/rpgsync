"""Commands written as functions or methods with named parameters.

    erase_screen("fade")
    change_hp(party, -10, lethal=False)
    learn_skill(actors[1], 5)
    pictures[36].show("fond", x=160, y=336, transparency=100)
    actors[2].set_sprite("Actor1", 3)
    this.flash(red=31, duration=0.5)

Each command is described by one or more :class:`Spec` (several specs may
share a command code, told apart by a hidden constant parameter, e.g.
learn/forget skill).  A spec is an ordered list of fields mapped onto the
command parameters.  Parameters past the known fields are kept verbatim in
``extra=(...)``, so any editor version or patch layout still round trips.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from .commands import Args, Call, Command, CompileError, Ctx, pystr

TRANSITIONS = [
    "fade",
    "random_blocks",
    "random_blocks_down",
    "random_blocks_up",
    "blinds",
    "vertical_stripes",
    "horizontal_stripes",
    "border_to_center",
    "center_to_border",
    "scroll_up",
    "scroll_down",
    "scroll_left",
    "scroll_right",
    "vertical_split",
    "horizontal_split",
    "cross_split",
    "zoom",
    "mosaic",
    "ripple",
    "instant",
    "none",
]
WEATHER = ["none", "rain", "snow", "fog", "sandstorm"]
STRENGTHS = ["weak", "medium", "strong"]
PICTURE_EFFECTS = ["none", "rotation", "wave"]
FLASH_MODES = ["once", "begin", "end"]
DIRECTIONS = ["up", "right", "down", "left"]
TELEPORT_DIRECTIONS = ["retain", "up", "right", "down", "left"]
STATS = ["max_hp", "max_sp", "attack", "defense", "spirit", "agility"]
EQUIP_SLOTS = ["weapon", "shield", "armor", "helmet", "accessory", "all"]
BGM_CONTEXTS = ["battle", "victory", "inn", "boat", "ship", "airship", "game_over"]
SE_CONTEXTS = [
    "cursor",
    "decision",
    "cancel",
    "buzzer",
    "battle_start",
    "escape",
    "enemy_attack",
    "enemy_damaged",
    "ally_damaged",
    "evade",
    "enemy_dies",
    "item",
]
TRANSITION_KINDS = [
    "teleport_erase",
    "teleport_show",
    "battle_start_erase",
    "battle_start_show",
    "battle_end_erase",
    "battle_end_show",
]
VEHICLES = ["boat", "ship", "airship"]
CHARACTERS = {10001: "player", 10002: "boat", 10003: "ship", 10004: "airship", 10005: "this"}

# width (number of parameters) of each field kind
WIDTH = {"xy": 3, "mxy": 4, "actor": 2, "value": 2, "optid": 2, "optvar": 2, "amount3": 2, "list": -1}


class Field(BaseModel):
    name: str
    default: Any = 0
    kind: str = "int"
    # int | bool | tenths | ms | enum | enum_m1 | xy (mode,x,y) | mxy (mode,map,x,y)
    # | actor (mode,id) | value (type,v) | sign (hidden 0/1 for a signed value)
    # | char | var | const (hidden, must equal default) | optid / optvar (flag,id)
    # | amount3 (type 0 const / 1 var / 2 percent, value; signed) | branches (handler flag)
    # | list (all remaining parameters)
    choices: list[str] = []
    positional: bool = False
    optional: bool = False  # may be absent (older editor versions)
    same_as: str | None = None  # default is the value of another field
    signed: str | None = None  # name of the sign field this value uses

    def width(self) -> int:
        return WIDTH.get(self.kind, 1)


class Spec(BaseModel):
    code: int
    name: str
    target: str | None = None  # pictures | actors | enemies | char | vehicle: params[0]
    string: str | None = None  # argument holding the command's string
    string_kw: bool = False  # string as keyword (omitted when empty)
    string_after: str | None = None  # positional string comes after this field
    fields: list[Field] = []
    extra: bool = True  # allow trailing parameters

    # -- decoding ---------------------------------------------------------------
    def decode(self, c: Command, ctx: Ctx) -> str | None:
        p = list(c.params)
        head = ""
        if self.target:
            if not p:
                return None
            head = self._target_src(p[0])
            if head is None:
                return None
            p = p[1:]
        if self.string is None and c.string:
            return None
        values: dict[str, Any] = {}
        raw: dict[str, int] = {}
        pos = 0
        missing = None
        for f in self.fields:
            w = len(p) - pos if f.kind == "list" else f.width()
            if pos + w > len(p) or (f.kind == "list" and pos > len(p)):
                missing = f.name
                break
            chunk = p[pos : pos + w]
            raw[f.name] = chunk[0] if chunk else 0
            v = self._dec_value(f, chunk, raw, ctx)
            if v is None:
                return None
            values[f.name] = v
            pos += w
        tail = p[pos:]
        if tail and not self.extra:
            return None
        args: list[str] = []
        kwargs: list[str] = []
        string_src = pystr(ctx.dec(c.string)) if self.string is not None else None
        if string_src is not None and not self.string_kw and self.string_after is None:
            args.append(string_src)
        for f in self.fields:
            if f.name not in values:
                continue
            v = values[f.name]
            if f.kind in ("const", "sign"):
                pass
            elif f.kind in ("xy", "mxy"):
                names = ["map", "x", "y"] if f.kind == "mxy" else ["x", "y"]
                if f.positional:
                    args.extend(v)
                else:
                    kwargs.extend("%s=%s" % nv for nv in zip(names, v))
            elif f.kind == "branches":
                if v != ("True" if ctx.match_header else "False"):
                    kwargs.append("%s=%s" % (f.name, v))
            elif f.positional:
                args.append(v)
            elif f.kind in ("optid", "optvar") and isinstance(v, tuple):
                kwargs.extend(["%s=%s" % (f.name, v[0]), "use_%s=False" % f.name])
            else:
                default = values.get(f.same_as) if f.same_as else self._src(f, f.default)
                if v != default or (f.optional and ctx.engine == "2k"):
                    kwargs.append("%s=%s" % (f.name, v))
            if self.string_after == f.name and string_src is not None:
                args.append(string_src)
        if string_src is not None and self.string_kw and string_src != '""':
            kwargs.append("%s=%s" % (self.string, string_src))
        if missing is not None and not (ctx.engine == "2k" and self._field(missing).optional):
            kwargs.append("%s=None" % missing)  # field absent in this (older) data
        if tail:
            kwargs.append("extra=(%s%s)" % (", ".join(str(x) for x in tail), "," if len(tail) == 1 else ""))
        return "%s%s(%s)" % (head, self.name, ", ".join(args + kwargs))

    def _field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)

    def _target_src(self, v: int) -> str | None:
        if self.target == "char":
            from . import pyexpr as P

            return P.char_src(v) + "."
        if self.target == "vehicle":
            return VEHICLES[v] + "." if 0 <= v < len(VEHICLES) else None
        return "%s[%d]." % (self.target, v)

    @staticmethod
    def _src(f: Field, value) -> str:
        if f.kind in ("enum", "enum_m1"):
            return pystr(value) if isinstance(value, str) else str(value)
        if f.kind in ("bool", "branches"):
            return "True" if value else "False"
        if value is None:
            return "None"
        return repr(value) if isinstance(value, float) else str(value)

    def _dec_value(self, f: Field, chunk: Sequence[int], raw: dict[str, int], ctx: Ctx) -> Any | None:
        from . import pyexpr as P

        v = chunk[0] if chunk else 0
        k = f.kind
        if k == "int":
            return str(v)
        if k in ("bool", "branches"):
            return {0: "False", 1: "True"}.get(v)
        if k == "tenths":
            return repr(v / 10)
        if k == "ms":
            return repr(v / 1000)
        if k in ("enum", "enum_m1"):
            i = v + 1 if k == "enum_m1" else v
            return pystr(f.choices[i]) if 0 <= i < len(f.choices) else str(v)
        if k == "const":
            return "" if v == f.default else None
        if k == "sign":
            return "" if v in (0, 1) else None
        if k == "var":
            return P.var_src(v)
        if k == "char":
            from . import pyexpr as P

            return P.char_src(v)
        if k == "xy":
            mode, x, y = chunk
            return {0: (str(x), str(y)), 1: (P.var_src(x), P.var_src(y))}.get(mode)
        if k == "mxy":
            mode, m, x, y = chunk
            return {
                0: (str(m), str(x), str(y)),
                1: (P.var_src(m), P.var_src(x), P.var_src(y)),
            }.get(mode)
        if k == "actor":
            mode, n = chunk
            return {0: "party" if n == 0 else None, 1: "actors[%d]" % n, 2: "actors[%s]" % P.var_src(n)}.get(mode)
        if k in ("value", "amount3"):
            kind, n = chunk
            src = {0: str(n), 1: P.var_src(n)}.get(kind)
            if k == "amount3" and kind == 2:
                src = "percent(%d)" % n
            if src is None:
                return None
            if f.signed is None:
                return src
            sign = raw[f.signed]
            if kind == 0 and (n < 0 or (sign == 1 and n == 0)):
                return None
            return ("-" if sign == 1 else "") + src
        if k in ("optid", "optvar"):
            flag, n = chunk
            src = str(n) if k == "optid" else P.var_src(n)
            if flag == 1:
                return src
            if flag == 0:
                return "None" if n == 0 else (src,)  # id kept while the option is off
            return None
        if k == "list":
            return "[%s]" % ", ".join(str(x) for x in chunk)
        raise ValueError(k)

    # -- encoding -----------------------------------------------------------------
    def arg_names(self) -> tuple[list[str], list[str]]:
        pos, kw = [], []
        if self.string is not None and not self.string_kw and self.string_after is None:
            pos.append(self.string)
        for f in self.fields:
            if f.kind in ("const", "sign"):
                continue
            names = ["map", "x", "y"] if f.kind == "mxy" else ["x", "y"] if f.kind == "xy" else [f.name]
            (pos if f.positional else kw).extend(names)
            if f.kind in ("optid", "optvar"):
                kw.append("use_" + f.name)
            if self.string_after == f.name:
                pos.append(self.string)
        if self.string_kw:
            kw.append(self.string)
        return pos, kw + ["extra"]

    def encode(self, call: Call, ctx: Ctx) -> Command:
        pos, kw = self.arg_names()
        a = Args(call, pos + kw)
        params: list[int] = []
        if self.target:
            params.append(self._target_id(call))
        enc: dict[str, int] = {}
        signs: dict[str, int] = {}
        # signs come from the values they belong to
        for f in self.fields:
            if f.signed is not None and f.name in a.values:
                signs[f.signed] = 1 if self._is_negative(a.get(f.name)) else 0
        for f in self.fields:
            absent = f.name in a.values and a.get(f.name) is None and f.kind not in ("optid", "optvar")
            if absent or (f.optional and ctx.engine == "2k" and f.name not in a.values):
                break
            params += self._enc_field(f, a, call, ctx, enc, signs)
        extra = a.get("extra")
        if extra is not None:
            if not (isinstance(extra, tuple) and all(isinstance(x, int) and not isinstance(x, bool) for x in extra)):
                raise CompileError("%s(): extra must be a tuple of integers" % self.name, call.node)
            params += list(extra)
        string = b""
        if self.string is not None:
            string = ctx.enc(a.str(self.string, ""), call.node)
        return Command(code=self.code, params=params, string=string)

    def _target_id(self, call: Call) -> int:
        from . import pyexpr as P

        t = call.target
        if self.target == "char":
            return P.need_char(t)
        if self.target == "vehicle":
            if isinstance(t, P.Sym) and t.name in VEHICLES:
                return VEHICLES.index(t.name)
            raise CompileError("%s() is called on boat, ship or airship" % self.name, call.node)
        n = P.int_index(t, self.target)
        if n is None:
            raise CompileError("%s() is called on %s[id]" % (self.name, self.target), call.node)
        return n

    @staticmethod
    def _is_negative(v) -> bool:
        from . import pyexpr as P

        return isinstance(v, P.Neg) or (isinstance(v, int) and not isinstance(v, bool) and v < 0)

    def _enc_field(
        self, f: Field, a: Args, call: Call, ctx: Ctx, enc: dict[str, int], signs: dict[str, int]
    ) -> list[int]:
        from . import pyexpr as P

        k = f.kind
        given = f.name in a.values
        v = a.get(f.name)
        if k == "const":
            return [f.default]
        if k == "sign":
            return [signs.get(f.name, 0)]
        if k == "branches":
            return [int(a.bool(f.name, ctx.match_header)) if given else int(ctx.match_header)]
        if k in ("xy", "mxy"):
            names = ["map", "x", "y"] if k == "mxy" else ["x", "y"]
            vals = [a.get(n, 0) for n in names]
            if all(P.var_index(x) is not None for x in vals):
                return [1] + [x.index for x in vals]
            if all(P.is_int(x) for x in vals):
                return [0] + vals
            raise CompileError(
                "%s(): %s are all numbers or all variables[id]" % (self.name, ", ".join(names)), call.node
            )
        if not given:
            if f.same_as:
                out = [enc[f.same_as]]
            else:
                out = self._default_params(f)
            if out:
                enc[f.name] = out[0]
            return out
        if k == "bool":
            out = [int(a.bool(f.name))]
        elif k in ("enum", "enum_m1"):
            if isinstance(v, str):
                if v not in f.choices:
                    raise CompileError(
                        "%s(): %s must be one of %s" % (self.name, f.name, ", ".join(f.choices)), call.node
                    )
                i = f.choices.index(v)
                out = [i - 1 if k == "enum_m1" else i]
            else:
                out = [a.int(f.name)]
        elif k in ("tenths", "ms"):
            scale = 10 if k == "tenths" else 1000
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise CompileError("%s(): %s is a number of seconds" % (self.name, f.name), call.node)
            n = round(v * scale)
            if abs(n - v * scale) > 1e-6:
                raise CompileError("%s(): %s must be a multiple of %s s" % (self.name, f.name, 1 / scale), call.node)
            out = [int(n)]
        elif k == "var":
            n = P.var_index(v)
            if n is None:
                raise CompileError("%s(): %s must be variables[id]" % (self.name, f.name), call.node)
            out = [n]
        elif k == "char":
            out = [P.need_char(v)]
        elif k == "actor":
            out = list(P._actor_target_val(v))
        elif k in ("value", "amount3"):
            if isinstance(v, (P.Neg, P.Pos)):
                v = v.value
            elif P.is_int(v) and v < 0:
                v = -v
            if k == "amount3" and isinstance(v, Call) and v.name == "percent" and v.target is None:
                out = [2, Args(v, ["percent"]).int("percent")]
            else:
                out = list(P.value_val(v, f.name))
        elif k in ("optid", "optvar"):
            use = a.get("use_" + f.name)
            if v is None:
                out = [0, 0]
            elif use is False:
                n = P.var_index(v) if k == "optvar" else (v if P.is_int(v) else None)
                if n is None:
                    raise CompileError("%s(): bad %s" % (self.name, f.name), call.node)
                out = [0, n]
            elif k == "optvar":
                n = P.var_index(v)
                if n is None:
                    raise CompileError("%s(): %s must be None or variables[id]" % (self.name, f.name), call.node)
                out = [1, n]
            else:
                out = [1, a.int(f.name)]
        elif k == "list":
            if not (isinstance(v, (list, tuple)) and all(P.is_int(x) for x in v)):
                raise CompileError("%s(): %s must be a list of ids" % (self.name, f.name), call.node)
            out = list(v)
        else:
            out = [a.int(f.name)]
        if out:
            enc[f.name] = out[0]
        return out

    def _default_params(self, f: Field) -> list[int]:
        k, d = f.kind, f.default
        if k == "list":
            return list(d or [])
        if k in ("optid", "optvar"):
            return [0, 0]
        if k in ("value", "amount3"):
            return [0, d or 0]
        if k == "actor":
            return [0, 0]
        if k in ("enum", "enum_m1"):
            i = f.choices.index(d) if isinstance(d, str) else d
            return [i - 1 if k == "enum_m1" and isinstance(d, str) else i]
        if k == "tenths":
            return [round(d * 10)]
        if k == "ms":
            return [round(d * 1000)]
        if k == "char":
            return [d]
        return [int(d)]


def F(name, default=0, kind="int", **kw) -> Field:
    return Field(name=name, default=default, kind=kind, **kw)


def P_(name, kind="int", default=0, **kw) -> Field:
    """positional field"""
    return Field(name=name, default=default, kind=kind, positional=True, **kw)


def const(value) -> Field:
    return Field(name="_const%d" % value, default=value, kind="const")


COLOR = [F("red", 100), F("green", 100), F("blue", 100)]
ACTOR = P_("actor", "actor")
SIGN = Field(name="_sign", kind="sign")


def amount(name="amount"):
    return P_(name, "value", signed="_sign")


SPECS: list[Spec] = [
    # -- screen ----------------------------------------------------------------------
    Spec(
        code=11010,
        name="erase_screen",
        extra=False,
        fields=[P_("transition", "enum_m1", "default", choices=["default"] + TRANSITIONS)],
    ),
    Spec(
        code=11020,
        name="show_screen",
        extra=False,
        fields=[P_("transition", "enum_m1", "default", choices=["default"] + TRANSITIONS)],
    ),
    Spec(
        code=11030,
        name="tint_screen",
        fields=COLOR + [F("saturation", 100), F("duration", 1.0, "tenths"), F("wait", False, "bool")],
    ),
    Spec(
        code=11040,
        name="flash_screen",
        fields=[
            F("red", 31),
            F("green", 31),
            F("blue", 31),
            F("strength", 31),
            F("duration", 0.5, "tenths"),
            F("wait", False, "bool"),
            F("mode", "once", "enum", choices=FLASH_MODES, optional=True),
        ],
    ),
    Spec(
        code=11050,
        name="shake_screen",
        fields=[
            F("strength", 3),
            F("speed", 3),
            F("duration", 1.0, "tenths"),
            F("wait", False, "bool"),
            F("mode", "once", "enum", choices=FLASH_MODES, optional=True),
        ],
    ),
    Spec(
        code=11070,
        name="weather",
        extra=False,
        fields=[P_("kind", "enum", "none", choices=WEATHER), F("strength", "weak", "enum", choices=STRENGTHS)],
    ),
    Spec(
        code=10690,
        name="set_transition",
        fields=[
            P_("kind", "enum", "teleport_erase", choices=TRANSITION_KINDS),
            P_("transition", "enum", "fade", choices=TRANSITIONS),
        ],
    ),
    # -- pictures -------------------------------------------------------------------
    Spec(
        code=11110,
        name="show",
        target="pictures",
        string="name",
        fields=[
            F("xy", kind="xy"),
            F("scroll_with_map", False, "bool"),
            F("magnify", 100),
            F("transparency", 0),
            F("transparent_color", True, "bool"),
        ]
        + COLOR
        + [
            F("saturation", 100),
            F("effect", "none", "enum", choices=PICTURE_EFFECTS),
            F("effect_power", 0),
            F("bottom_transparency", same_as="transparency", optional=True),
        ],
    ),
    Spec(
        code=11120,
        name="move",
        target="pictures",
        fields=[F("xy", kind="xy"), F("p4", 0), F("magnify", 100), F("transparency", 0), F("p7", 0)]
        + COLOR
        + [
            F("saturation", 100),
            F("effect", "none", "enum", choices=PICTURE_EFFECTS),
            F("effect_power", 0),
            F("duration", 0.0, "tenths"),
            F("wait", False, "bool"),
            F("bottom_transparency", same_as="transparency", optional=True),
        ],
    ),
    Spec(code=11130, name="erase", target="pictures"),
    # -- audio / system --------------------------------------------------------------------
    Spec(code=11520, name="fade_out_bgm", extra=False, fields=[P_("seconds", "ms", 0.0)]),
    Spec(
        code=10660,
        name="set_system_bgm",
        string="name",
        string_after="context",
        fields=[
            P_("context", "enum", "battle", choices=BGM_CONTEXTS),
            F("fade", 0),
            F("volume", 100),
            F("tempo", 100),
            F("balance", 50),
        ],
    ),
    Spec(
        code=10670,
        name="set_system_se",
        string="name",
        string_after="context",
        fields=[
            P_("context", "enum", "cursor", choices=SE_CONTEXTS),
            F("volume", 100),
            F("tempo", 100),
            F("balance", 50),
        ],
    ),
    Spec(
        code=10680,
        name="set_system_graphics",
        string="name",
        fields=[
            F("stretch", "stretch", "enum", choices=["stretch", "tile"], optional=True),
            F("font", "gothic", "enum", choices=["gothic", "mincho"], optional=True),
        ],
    ),
    Spec(code=11560, name="play_movie", string="name", fields=[F("xy", kind="xy"), F("width", 320), F("height", 240)]),
    # -- party / actors ------------------------------------------------------------------
    Spec(code=10430, name="change_stat", fields=[ACTOR, SIGN, P_("stat", "enum", "max_hp", choices=STATS), amount()]),
    Spec(code=10440, name="learn_skill", fields=[ACTOR, const(0), P_("skill", "value")]),
    Spec(code=10440, name="forget_skill", fields=[ACTOR, const(1), P_("skill", "value")]),
    Spec(code=10450, name="equip", fields=[ACTOR, const(0), P_("item", "value")]),
    Spec(
        code=10450, name="unequip", fields=[ACTOR, const(1), P_("slot", "enum", "all", choices=EQUIP_SLOTS), F("p4", 0)]
    ),
    Spec(code=10460, name="change_hp", fields=[ACTOR, SIGN, amount(), F("lethal", False, "bool")]),
    Spec(code=10470, name="change_sp", fields=[ACTOR, SIGN, amount()]),
    Spec(code=10480, name="add_state", fields=[ACTOR, const(0), P_("state")]),
    Spec(code=10480, name="remove_state", fields=[ACTOR, const(1), P_("state")]),
    Spec(
        code=10500,
        name="simulate_attack",
        fields=[
            ACTOR,
            F("attack", 0),
            F("defense", 0),
            F("spirit", 0),
            F("variance", 0),
            F("store_damage", None, "optvar"),
        ],
    ),
    Spec(
        code=10630,
        name="set_sprite",
        target="actors",
        string="charset",
        fields=[P_("index"), F("transparent", False, "bool")],
    ),
    Spec(code=10640, name="set_face", target="actors", string="faceset", fields=[P_("index")]),
    Spec(code=10650, name="set_sprite", target="vehicle", string="charset", fields=[P_("index")]),
    Spec(
        code=10740, name="enter_hero_name", fields=[P_("actor"), F("charset", 0), F("use_current_name", False, "bool")]
    ),
    Spec(
        code=1008,
        name="change_class",
        fields=[
            ACTOR,
            P_("class_id"),
            F("reset_level", False, "bool"),
            F("skills", "keep", "enum", choices=["keep", "replace", "add"]),
            F("stats", "keep", "enum", choices=["keep", "halve", "level1", "current_level"]),
            F("message", False, "bool"),
        ],
    ),
    Spec(code=1009, name="remove_battle_command", fields=[ACTOR, P_("command"), const(0)]),
    Spec(code=1009, name="add_battle_command", fields=[ACTOR, P_("command"), const(1)]),
    # -- map / movement ---------------------------------------------------------------------
    Spec(code=10820, name="memorize_location", fields=[P_("map", "var"), P_("x", "var"), P_("y", "var")]),
    Spec(code=10830, name="recall_location", fields=[P_("map", "var"), P_("x", "var"), P_("y", "var")]),
    Spec(code=10840, name="toggle_vehicle"),
    Spec(
        code=10850,
        name="set_vehicle_location",
        fields=[
            P_("vehicle", "enum_m1", "party", choices=["party"] + VEHICLES),
            P_("mxy", "mxy"),
            F("direction", "retain", "enum", choices=TELEPORT_DIRECTIONS, optional=True),
        ],
    ),
    Spec(code=10870, name="swap_events", fields=[P_("first", "char"), P_("second", "char")]),
    Spec(
        code=11320,
        name="flash",
        target="char",
        fields=[
            F("red", 31),
            F("green", 31),
            F("blue", 31),
            F("strength", 31),
            F("duration", 0.5, "tenths"),
            F("wait", False, "bool"),
        ],
    ),
    Spec(code=11710, name="set_tileset", fields=[P_("chipset")]),
    Spec(
        code=11720,
        name="set_panorama",
        string="name",
        fields=[
            F("scroll_x", False, "bool"),
            F("scroll_y", False, "bool"),
            F("auto_scroll_x", False, "bool"),
            F("speed_x", 0),
            F("auto_scroll_y", False, "bool"),
            F("speed_y", 0),
        ],
    ),
    Spec(code=11740, name="set_encounter_rate", fields=[P_("steps")]),
    Spec(
        code=11750,
        name="replace_tile",
        fields=[P_("layer", "enum", "lower", choices=["lower", "upper"]), P_("old"), P_("new")],
    ),
    Spec(
        code=11810,
        name="add_teleport_target",
        fields=[const(0), P_("map"), P_("x"), P_("y"), F("switch", None, "optid")],
    ),
    Spec(
        code=11810,
        name="remove_teleport_target",
        fields=[const(1), P_("map"), F("x", 0), F("y", 0), F("switch", None, "optid")],
    ),
    Spec(code=11830, name="set_escape_target", fields=[P_("map"), P_("x"), P_("y"), F("switch", None, "optid")]),
    # -- shops, inns, battles (used with `match` when they have branches) ---------------------
    Spec(
        code=10710,
        name="start_battle",
        string="image",
        string_kw=True,
        fields=[
            P_("troop", "value"),
            F("background", "map", "enum", choices=["map", "image", "terrain"]),
            F("escape", "disallow", "enum", choices=["disallow", "end_event", "branch"]),
            F("defeat", "game_over", "enum", choices=["game_over", "branch"]),
            F("first_strike", False, "bool"),
            F(
                "condition",
                "none",
                "enum",
                choices=["none", "initiative", "back_attack", "surround", "pincer"],
                optional=True,
            ),
            F("formation", "terrain", "enum", choices=["terrain", "loose", "tight"], optional=True),
            F("terrain", 0, optional=True),
        ],
    ),
    Spec(
        code=10720,
        name="open_shop",
        fields=[
            F("mode", "buy_sell", "enum", choices=["buy_sell", "buy", "sell"]),
            F("style", 0),
            F("branches", False, "branches"),
            F("p3", 0),
            P_("items", "list", []),
        ],
    ),
    Spec(code=10730, name="inn", fields=[F("style", 0), P_("price"), F("branches", False, "branches")]),
    # -- battle ----------------------------------------------------------------------------
    Spec(
        code=13110,
        name="change_hp",
        target="enemies",
        fields=[
            SIGN,
            Field(name="amount", kind="amount3", positional=True, signed="_sign"),
            F("lethal", False, "bool"),
        ],
    ),
    Spec(code=13120, name="change_sp", target="enemies", fields=[SIGN, amount()]),
    Spec(code=13130, name="add_state", target="enemies", fields=[const(0), P_("state")]),
    Spec(code=13130, name="remove_state", target="enemies", fields=[const(1), P_("state")]),
    Spec(code=13150, name="show", target="enemies"),
    Spec(code=13210, name="set_battle_background", string="name"),
    Spec(
        code=13260,
        name="show_battle_animation",
        fields=[P_("animation"), P_("target"), F("wait", False, "bool"), F("allies", False, "bool", optional=True)],
    ),
    Spec(code=13410, name="end_battle"),
    Spec(code=1005, name="battle_call_common_event", fields=[P_("common_event")]),
    Spec(
        code=1006,
        name="force_flee",
        fields=[
            P_("who", "enum", "party", choices=["party", "all_enemies", "enemy"]),
            F("enemy", 0),
            F("ignore_conditions", False, "bool"),
        ],
    ),
    Spec(code=1007, name="enable_combo", fields=[P_("actor"), P_("command"), P_("times")]),
    # -- system menus (2003) -----------------------------------------------------------------
    Spec(code=5001, name="open_load_menu"),
    Spec(code=5002, name="exit_game"),
    Spec(code=5003, name="toggle_atb_mode"),
    Spec(code=5004, name="toggle_fullscreen"),
    Spec(code=5005, name="open_video_options"),
]

PAN = {0: "lock_screen", 1: "unlock_screen", 2: "pan_screen", 3: "reset_screen_pan"}

BY_CODE: dict[int, list[Spec]] = {}
for _s in SPECS:
    BY_CODE.setdefault(_s.code, []).append(_s)
FUNCTIONS: dict[str, Spec] = {s.name: s for s in SPECS if s.target is None}
METHODS: dict[tuple[str, str], Spec] = {(s.target, s.name): s for s in SPECS if s.target}
MATCH_HEADERS = {10710: "start_battle", 10720: "open_shop", 10730: "inn"}


def decode(c: Command, ctx: Ctx) -> str | None:
    if c.code == 11060:
        return _decode_pan(c)
    for spec in BY_CODE.get(c.code, []):
        src = spec.decode(c, ctx)
        if src is not None:
            return src
    return None


def encode(call: Call, ctx: Ctx) -> Command | None:
    from . import pyexpr as P

    if call.target is None:
        if call.name in PAN.values():
            return _encode_pan(call)
        spec = FUNCTIONS.get(call.name)
    else:
        t = call.target
        if isinstance(t, P.Ref):
            key = t.coll
        elif isinstance(t, P.Sym) and t.name in VEHICLES and (("vehicle", call.name) in METHODS):
            key = "vehicle"
        else:
            key = "char" if P.char_id(t) is not None else None
        spec = METHODS.get((key, call.name)) or (METHODS.get(("char", call.name)) if key == "events" else None)
    return spec.encode(call, ctx) if spec else None


PAN_DEFAULTS = {0: (0, 0, 0, 0), 1: (0, 0, 0, 0), 2: (0, 0, 3, 0), 3: (0, 0, 3, 0)}


def _decode_pan(c: Command) -> str | None:
    p = c.params
    if len(p) != 5 or p[0] not in PAN or not 0 <= p[1] < 4:
        return None
    kind = p[0]
    values = {
        "direction": pystr(DIRECTIONS[p[1]]),
        "distance": str(p[2]),
        "speed": str(p[3]),
        "wait": {0: "False", 1: "True"}.get(p[4]),
    }
    if values["wait"] is None:
        return None
    d = PAN_DEFAULTS[kind]
    defaults = {
        "direction": pystr(DIRECTIONS[d[0]]),
        "distance": str(d[1]),
        "speed": str(d[2]),
        "wait": "True" if d[3] else "False",
    }
    args = []
    if kind == 2:
        args = [values.pop("direction"), values.pop("distance")]
    kwargs = ["%s=%s" % (k, v) for k, v in values.items() if v != defaults[k]]
    return "%s(%s)" % (PAN[kind], ", ".join(args + kwargs))


def _encode_pan(call: Call) -> Command:
    kind = {v: k for k, v in PAN.items()}[call.name]
    d = PAN_DEFAULTS[kind]
    a = Args(call, ["direction", "distance", "speed", "wait"])
    direction = a.enum("direction", DIRECTIONS, DIRECTIONS[d[0]]) if kind == 2 or "direction" in a.values else d[0]
    return Command(
        code=11060,
        params=[kind, direction, a.int("distance", d[1]), a.int("speed", d[2]), int(a.bool("wait", bool(d[3])))],
    )
