"""Maniac Patch commands, written ``maniac.name(...)`` like DynRPG's ``dyn.name(...)``.

    maniac.get_mouse_position(x=variables.mouse_x, y=variables.mouse_y)
    maniac.save(slot=variables.slot, result=variables.ok)
    maniac.control_strings(0, 1, 2, text="...")   # no named form yet: raw parameters

The Maniac Patch (https://github.com/EasyRPG/Player/issues/1818) adds event
commands 3001-3038.  Commands with a known layout have named arguments (specs
in this module, decoded by :mod:`rpgsync.named`); any other Maniac command is
still written by name, with its parameters as positional numbers.  Both forms
compile back to the same bytes.

Maniac packs "how is this value given" selectors into one parameter, 4 bits
per value: 0 a number, 1 ``variables[n]``, 2 ``variables[variables[n]]``.
Named arguments take any of the three forms and the selectors follow.
"""

from __future__ import annotations

from pydantic import BaseModel

from . import commands as K
from . import named
from .commands import Call, Command, CompileError, Ctx, pystr
from .named import F, Field, Spec

NAMESPACE = "maniac"

# codes from liblcf (lcf/rpg/eventcommand.h); later Maniac versions added more
NAMES = {
    3001: "get_save_info",
    3002: "save",
    3003: "load",
    3004: "end_load_process",
    3005: "get_mouse_position",
    3006: "set_mouse_position",
    3007: "show_string_picture",
    3008: "get_picture_info",
    3009: "control_battle",
    3010: "control_atb_gauge",
    3011: "change_battle_command_ex",
    3012: "get_battle_info",
    3013: "control_var_array",
    3014: "key_input_proc_ex",
    3015: "rewrite_map",
    3016: "control_global_save",
    3017: "change_picture_id",
    3018: "set_game_option",
    3019: "call_command",
    3020: "control_strings",
    3021: "get_game_info",
    3025: "edit_picture",
    3026: "write_picture",
    3027: "add_move_route",
    3028: "edit_tile",
    3029: "control_text_processing",
    3030: "script",  # later codes: names from the Maniac test game, not in liblcf
    3031: "script_line",
    3032: "zoom",
    3033: "console",
    3036: "control_self_variable",
    3038: "control_media_option",
}
CODES = range(3000, 4000)


def name_of(code: int) -> str:
    return NAMES.get(code, "command_%d" % code)


def code_of(name: str) -> int | None:
    for code, n in NAMES.items():
        if n == name:
            return code
    if name.startswith("command_") and name[8:].isdigit() and int(name[8:]) in CODES:
        return int(name[8:])
    return None


def decode_forms(c: Command, ctx: Ctx):
    """The typed forms of a Maniac command that are no named spec (Control Strings,
    Console...)."""
    dec = {3020: decode_strings, 3021: _decode_game_info, 3033: _decode_console, 3038: _decode_media}.get(c.code)
    src = dec(c, ctx) if dec else None
    if src is not None:
        yield src


def encode_form(call: Call, ctx: Ctx) -> Command | None:
    """A typed form that is no named spec (maniac.console_print()...), None for another name."""
    enc = FORM_ENCODERS.get(call.name)
    return enc(call, ctx) if enc else None


def decode_generic(c: Command, ctx: Ctx) -> str | None:
    """Any Maniac command: ``maniac.name(p0, p1, ..., text="...")``."""
    if c.code not in CODES:
        return None
    if not c.params and any(s.code == c.code for s in SPECS):
        return None  # would read as the named form with its defaults
    args = [str(p) for p in c.params]
    if c.string:
        args.append("text=%s" % pystr(ctx.dec(c.string)))
    return "%s.%s(%s)" % (NAMESPACE, name_of(c.code), ", ".join(args))


def encode_generic(call: Call, ctx: Ctx) -> Command | None:
    code = code_of(call.name)
    if code is None:
        return None
    for p in call.args:
        if not isinstance(p, int) or isinstance(p, bool):
            raise CompileError(
                "%s.%s(): without names, the parameters are numbers (the command's raw parameters), got %r"
                % (NAMESPACE, call.name, p),
                call.node,
            )
    unknown = set(call.kwargs) - {"text"}
    if unknown:
        raise CompileError(
            "%s.%s() got %s: write its raw parameters as numbers, and text=..."
            % (NAMESPACE, call.name, ", ".join(sorted(unknown))),
            call.node,
        )
    text = call.kwargs.get("text", "")
    if not isinstance(text, str):
        raise CompileError("%s.%s(): text must be a string" % (NAMESPACE, call.name), call.node)
    return Command(code=code, params=list(call.args), string=ctx.enc(text, call.node))


# --------------------------------------------------------------------------
# Named forms
# --------------------------------------------------------------------------


def modes(name: str = "_modes") -> Field:
    """The hidden parameter holding the value selectors (and flags)."""
    return Field(name=name, kind="modes")


def value(name: str, modes: str = "_modes", slot: int = 0, default=0) -> Field:
    """A value given as a number, variables[n], variables[variables[n]] or switches[n];
    its selector is the slot-th 4 bits of `modes`."""
    return Field(name=name, kind="vref", modes=modes, shift=4 * slot, default=default)


def flag(name: str, modes: str, bit: int, bits: int = 1, choices: list[str] | None = None, default=0) -> Field:
    """Bits of a packed parameter: a bool, or a name from choices."""
    if choices and default == 0:
        default = choices[0]
    return Field(name=name, kind="bits", modes=modes, shift=bit, bits=bits, choices=choices or [], default=default)


def var(name: str) -> Field:
    return F(name, kind="var")


def enum(name: str, choices: list[str]) -> Field:
    return Field(name=name, kind="enum", choices=choices, default=choices[0], strict=True)


def M(code: int, *fields: Field, **kw) -> Spec:
    return Spec(code=code, name=NAMES[code], namespace=NAMESPACE, fields=list(fields), **kw)


BATTLE_TARGETS = ["actor", "member", "party", "enemy", "troop"]

SPECS: list[Spec] = [
    # -- saves ------------------------------------------------------------------------
    M(
        3001,
        modes("_slot_mode"),
        value("slot", "_slot_mode"),
        flag("leader_name", "_slot_mode", 4),
        var("date"),
        var("time"),
        var("level"),
        var("hp"),
        F("name_string"),
        modes("_face_mode"),
        *(value("face%d" % n, "_face_mode") for n in range(1, 5)),
    ),
    M(3002, modes("_slot_mode"), value("slot", "_slot_mode"), F("result", None, "optvar")),
    M(
        3003,
        modes("_slot_mode"),
        value("slot", "_slot_mode"),
        F("skip_check", False, "bool"),
        F("no_blackout", False, "bool"),
        min_params=3,
    ),
    M(3004),
    M(
        3016,
        enum("op", ["open", "close", "save", "save_close", "load_values", "store_values"]),
        modes(),
        enum("kind", ["switches", "variables"]),
        value("game_id", slot=0),
        value("global_id", slot=1),
        value("count", slot=2),
        min_params=1,
    ),  # fmt: skip
    # -- mouse and keys ---------------------------------------------------------------
    M(3005, var("x"), var("y")),
    M(3006, modes("_xy_mode"), value("x", "_xy_mode"), value("y", "_xy_mode")),
    M(
        3014,
        enum("op", ["keyboard", "keyboard_raw", "key", "joypad", "joypad_raw", "remap_joypad"]),
        var("output"),
        modes("_key_mode"),
        value("key", "_key_mode"),
        min_params=2,
    ),  # fmt: skip
    # -- pictures ---------------------------------------------------------------------
    M(
        3008,
        modes(),
        enum("info", ["original_size", "current_size", "target"]),
        enum("origin", ["center", "top_left", "edges"]),
        value("picture"),
        var("x"),
        var("y"),
        var("width"),
        var("height"),
    ),  # fmt: skip
    M(
        3017,
        enum("op", ["move", "swap", "slide"]),
        modes(),
        value("first", slot=0),
        value("count", slot=1),
        value("target", slot=2),
        F("ignore_out_of_range", False, "bool"),
    ),  # fmt: skip
    M(
        3025,
        modes(),
        value("picture", slot=0),
        value("x", slot=1),
        value("y", slot=2),
        value("width", slot=3),
        value("height", slot=4),
        value("first_var", slot=5),
        modes("_flags"),
        flag("opaque", "_flags", 0),
        flag("keep_area", "_flags", 1),
    ),  # fmt: skip
    M(
        3026,
        modes(),
        flag("name_from", "_modes", 4, 4, ["text", "string", "string_ref"]),
        enum("target", ["screen", "picture"]),
        value("picture", slot=0),
        F("name_string"),
        modes("_flags"),
        flag("dynamic", "_flags", 0),
        flag("opaque", "_flags", 1),
        string="filename",
        string_kw=True,
    ),
    M(
        3028,
        modes(),
        value("picture", slot=0),
        value("x", slot=1),
        value("y", slot=2),
        value("width", slot=3),
        value("height", slot=4),
        value("tile", slot=5),
        modes("_flags"),
        flag("no_autotile", "_flags", 0),
        flag("clear", "_flags", 1),
        F("tiles_from_vars", False, "bool"),
        enum("layer", ["lower", "upper"]),
        value("tileset", slot=6),
        value("pattern", slot=7),
    ),  # fmt: skip
    # -- map and screen ---------------------------------------------------------------
    M(
        3015,
        modes(),
        F("tiles_from_vars", False, "bool"),
        enum("layer", ["lower", "upper"]),
        value("tile", slot=0),
        value("x", slot=1),
        value("y", slot=2),
        value("width", slot=3),
        value("height", slot=4),
        F("no_autotile", False, "bool"),
    ),  # fmt: skip
    M(
        3032,
        modes(),
        value("x", slot=0),
        value("y", slot=1),
        value("scale", slot=2),
        value("duration", slot=3),
        value("layer", slot=4),
        F("wait", False, "bool"),
    ),  # fmt: skip
    # -- variables and options --------------------------------------------------------
    M(
        3013,
        enum(
            "op",
            [
                "copy",
                "swap",
                "sort",
                "sort_descending",
                "shuffle",
                "enumerate",
                "add",
                "sub",
                "mul",
                "div",
                "mod",
                "or",
                "and",
                "xor",
                "shl",
                "shr",
            ],
        ),
        modes(),
        value("a", slot=0),
        value("length", slot=1),
        value("b", slot=2),
    ),  # fmt: skip
    M(
        3018,
        modes("_value_mode"),
        enum(
            "option",
            [
                "run_when_inactive",
                "fps",
                "picture_limit",
                "frame_skip",
                "mouse_messages",
                "battle_origin",
                "battle_animation_limit",
                "face_size",
            ],
        ),
        value("value", "_value_mode"),
        F("value2"),
        min_params=3,
    ),  # fmt: skip
    M(
        3029,
        enum("source", ["id", "variable", "variable_ref", "name", "string", "string_ref"]),
        modes("_hooks"),
        flag("on_marker", "_hooks", 0),
        flag("on_open", "_hooks", 1),
        flag("on_close", "_hooks", 2),
        flag("on_char", "_hooks", 3),
        F("event"),
        F("info_var"),
        F("info_string"),
        F("number_args"),
        F("string_args"),
        string="name",
        string_kw=True,
    ),  # fmt: skip
    # -- battle -----------------------------------------------------------------------
    M(
        3009,
        enum("hook", ["atb", "damage", "targeting", "state", "stat_change"]),
        modes("_ce_mode"),
        value("common_event", "_ce_mode"),
        var("first_var"),
    ),  # fmt: skip
    M(
        3010,
        enum("target", BATTLE_TARGETS),
        modes("_target_mode"),
        value("target_id", "_target_mode"),
        enum("op", ["set", "add", "sub"]),
        enum("unit", ["value", "percent"]),
        modes("_value_mode"),
        value("value", "_value_mode"),
    ),  # fmt: skip
    M(
        3011,
        F("disable_row", False, "bool"),
        modes("_commands"),
        flag("no_fight", "_commands", 0),
        flag("no_auto", "_commands", 1),
        flag("no_escape", "_commands", 2),
        flag("win", "_commands", 3),
        flag("lose", "_commands", 4),
    ),  # fmt: skip
    M(
        3012,
        enum("target", BATTLE_TARGETS),
        F("info"),
        modes("_target_mode"),
        value("target_id", "_target_mode"),
        var("first_var"),
    ),  # fmt: skip
]


# -- Show String Picture (3007) -----------------------------------------------------
#
# p0 selectors: picture, x/y, magnify, transparency, width/height, font size (4 bits each),
# then the origin (bits 24-27); p12 blending and flips; p13 window style; p14 transparent
# color, letter and line spacing (8 bits each); p17 like a picture's flags.  The text is
# "\x01TEXT\x01SYSTEM\x01FONT" (\x02 / \x03 before an argument read from a string
# variable: kept raw).

STRING_PICTURE_EFFECTS = ["none", "rotation", "wave", "fixed_angle"]


class StringPictureSpec(Spec):
    """Show String Picture: the command's string holds the text, the system graphic and the font."""

    def arg_names(self) -> tuple[list[str], list[str]]:
        pos, kw = super().arg_names()
        return pos, kw + ["system", "font"]

    def decode(self, c: Command, ctx: Ctx) -> str | None:
        parts = c.string.split(b"\x01")
        if len(parts) != 4 or parts[0] or any(b in part for part in parts for b in (b"\x02", b"\x03")):
            return None
        text, system, font = parts[1:]
        src = super().decode(Command(code=c.code, params=c.params, string=text), ctx)
        if src is None:
            return None
        more = ["%s=%s" % (k, pystr(ctx.dec(v))) for k, v in (("system", system), ("font", font)) if v]
        if not more:
            return src
        head, inner = src[: src.index("(")], src[src.index("(") + 1 : -1]
        return "%s(%s)" % (head, ", ".join(([inner] if inner else []) + more))

    def encode(self, call: Call, ctx: Ctx) -> Command:
        kwargs = dict(call.kwargs)
        names = {}
        for k in ("system", "font"):
            v = kwargs.pop(k, "")
            if not isinstance(v, str):
                raise CompileError("%s.%s(): %s is a name (a string)" % (NAMESPACE, self.name, k), call.node)
            names[k] = ctx.enc(v, call.node)
        cmd = super().encode(
            Call(name=call.name, args=call.args, kwargs=kwargs, target=call.target, node=call.node), ctx
        )
        cmd.string = b"\x01" + cmd.string + b"\x01" + names["system"] + b"\x01" + names["font"]
        return cmd


SPECS.append(
    StringPictureSpec(
        code=3007,
        name=NAMES[3007],
        namespace=NAMESPACE,
        string="text",
        string_kw=True,
        min_params=21,
        fields=[
            modes(),
            flag("origin", "_modes", 24, 4),
            value("picture", slot=0),
            value("x", slot=1),
            value("y", slot=1),
            value("magnify", slot=2, default=100),
            value("transparency", slot=3),
            F("red", 100),
            F("green", 100),
            F("blue", 100),
            F("saturation", 100),
            enum("effect", STRING_PICTURE_EFFECTS),
            F("effect_power"),
            modes("_blend"),
            flag("blend", "_blend", 0, 4, ["normal", "multiply", "additive", "overlay"]),
            flag("flip_x", "_blend", 4),
            flag("flip_y", "_blend", 5),
            modes("_style"),
            flag("scroll_with_map", "_style", 0),
            flag("stretch", "_style", 8, 4, ["none", "stretch", "tile"], default="stretch"),
            flag("no_frame", "_style", 12),
            flag("no_gradient", "_style", 13),
            flag("no_shadow", "_style", 14),
            flag("bold", "_style", 15),
            flag("no_margin", "_style", 16),
            modes("_text_style"),
            flag("transparent_color", "_text_style", 0, default=True),
            flag("letter_spacing", "_text_style", 8, 8),
            flag("line_spacing", "_text_style", 16, 8, default=4),
            F("map_layer", 7),
            F("battle_layer", 0),
            modes("_flags"),
            flag("erase_on_map_change", "_flags", 0, default=True),
            flag("erase_on_battle_end", "_flags", 1),
            flag("affected_by_tint", "_flags", 4),
            flag("affected_by_flash", "_flags", 5, default=True),
            flag("affected_by_shake", "_flags", 6, default=True),
            value("width", slot=4),
            value("height", slot=4),
            value("font_size", slot=5, default=12),
            F("angle_divisor"),
        ],
    )
)

named.register(SPECS)


# --------------------------------------------------------------------------
# Console (3033)
# --------------------------------------------------------------------------
#
#     maniac.console_open()
#     maniac.console_print("Hello \\v[1]", expand=True)    # or strings[n], strings[variables[n]]
#     maniac.console_input(strings[1], max_length=20)
#     maniac.console_color(foreground=variables[1], background=4)
#
# p0 selectors of p3 (bits 8-11) and p4 (bits 12-15); p1 0 open, 1 close, 2 print,
# 3 input, 4 color; p2 flags: 1 expand \\v[n]..., 2 hexadecimal, 4 no line break, 8 set
# the foreground, 16 the background; p3, p4 the arguments (colors: console attributes,
# 1 blue, 2 green, 4 red, 8 intense).

_CONSOLE_PRINT_FLAGS = {1: "expand", 2: "hex", 4: "no_newline"}


def _decode_console(c: Command, ctx: Ctx) -> str | None:
    from . import maniac_ext as X
    from . import pyexpr as P

    p = c.params
    if len(p) != 5 or p[0] & ~0xFF00:
        return None
    m1, m2, op, flags, a, b = (p[0] >> 8) & 0xF, (p[0] >> 12) & 0xF, p[1], p[2], p[3], p[4]
    if op in (0, 1):
        if p[0] or flags or a or b or c.string:
            return None
        return "%s.console_%s()" % (NAMESPACE, ("open", "close")[op])
    if op == 2:
        if m2 or b or flags & ~7:
            return None
        src = X.text_src(m1, a, ctx.dec(c.string))
        kw = ["%s=True" % n for bit, n in _CONSOLE_PRINT_FLAGS.items() if flags & bit]
        return None if src is None else "%s.console_print(%s)" % (NAMESPACE, ", ".join([src] + kw))
    if c.string:
        return None
    if op == 3:
        if flags & ~3 or m1 > 1 or m2 > 2:
            return None
        into = P.str_src(a) if m1 == 0 else "strings[%s]" % P.var_src(a)
        kw = ["max_length=%s" % P.mval_src(m2, b)] if m2 or b else []
        kw += ["%s=True" % n for bit, n in _CONSOLE_PRINT_FLAGS.items() if flags & bit]
        return "%s.console_input(%s)" % (NAMESPACE, ", ".join([into] + kw))
    if op == 4:
        if flags & ~24 or m1 > 2 or m2 > 2:
            return None
        kw = []
        for bit, name, mode, v in ((8, "foreground", m1, a), (16, "background", m2, b)):
            if flags & bit:
                kw.append("%s=%s" % (name, P.mval_src(mode, v)))
            elif mode or v:
                return None
        return "%s.console_color(%s)" % (NAMESPACE, ", ".join(kw))
    return None


def _console_flags(a: K.Args, names: dict[int, str]) -> int:
    return sum(bit for bit, n in names.items() if a.bool(n))


def _encode_console(call: Call, ctx: Ctx) -> Command:
    from . import maniac_ext as X
    from . import pyexpr as P

    name = call.name[len("console_") :]
    if name in ("open", "close"):
        K.Args(call, [])
        return Command(code=3033, params=[0, ("open", "close").index(name), 0, 0, 0])
    if name == "print":
        a = K.Args(call, ["text"] + list(_CONSOLE_PRINT_FLAGS.values()))
        mode, n, text = X.text_val(a.get("text", ""), "console_print(): the text", call.node)
        return Command(
            code=3033,
            params=[mode << 8, 2, _console_flags(a, _CONSOLE_PRINT_FLAGS), n, 0],
            string=ctx.enc(text, call.node),
        )
    if name == "input":
        a = K.Args(call, ["into", "max_length", "expand", "hex"])
        mode, n, text = X.text_val(a.req("into"), "console_input(): into", call.node)
        if mode == 0:
            raise CompileError("console_input(): into is strings[id] or strings[variables[id]]", call.node)
        m2, length = P.mval_val(a.get("max_length", 0), "max_length", modes=3)
        flags = _console_flags(a, {1: "expand", 2: "hex"})
        return Command(code=3033, params=[(mode - 1) << 8 | m2 << 12, 3, flags, n, length])
    a = K.Args(call, ["foreground", "background"])
    flags, modes, values = 0, 0, []
    for k, (bit, key) in enumerate(((8, "foreground"), (16, "background"))):
        mode, v = P.mval_val(a.get(key, 0), key, modes=3) if key in a.values else (0, 0)
        flags |= bit if key in a.values else 0
        modes |= mode << (8 + 4 * k)
        values.append(v)
    return Command(code=3033, params=[modes, 4, flags] + values)


# --------------------------------------------------------------------------
# Control Media Option (3038)
# --------------------------------------------------------------------------
#
#     maniac.get_media_option("window_zoom", into=variables[1])
#     maniac.set_media_option("show_fps", 1)
#
# p0 selector of p3 (bits 8-11; get: 0 variables[n], 1 variables[variables[n]]); p1 0 get,
# 1 set; p2 the option; p3 the value or variable.

MEDIA_OPTIONS = [
    "screen_mode",
    "window_zoom",
    "window_interpolation",
    "keep_size",
    "fullscreen_interpolation",
    "uneven_pixels",
    "ignore_aspect_ratio",
    "show_fps",
    "save_as_default",
    "volume",
]


def _decode_media(c: Command, ctx: Ctx) -> str | None:
    from . import pyexpr as P

    p = c.params
    if len(p) != 4 or c.string or p[0] & ~0xF00 or p[1] not in (0, 1) or not 0 <= p[2] < len(MEDIA_OPTIONS):
        return None
    mode = p[0] >> 8
    option = pystr(MEDIA_OPTIONS[p[2]])
    if p[1] == 0:
        if mode > 1:
            return None
        into = P.var_src(p[3]) if mode == 0 else "variables[%s]" % P.var_src(p[3])
        return "%s.get_media_option(%s, into=%s)" % (NAMESPACE, option, into)
    src = P.mval_src(mode, p[3]) if mode <= 2 else None
    return None if src is None else "%s.set_media_option(%s, %s)" % (NAMESPACE, option, src)


def _encode_media(call: Call, ctx: Ctx) -> Command:
    from . import pyexpr as P

    get = call.name == "get_media_option"
    a = K.Args(call, ["option", "into" if get else "value"])
    option = a.enum("option", MEDIA_OPTIONS)
    if get:
        mode, n = P.mval_val(a.req("into"), "into", modes=3)
        if mode == 0:
            raise CompileError("get_media_option(): into is variables[id] or variables[variables[id]]", call.node)
        return Command(code=3038, params=[(mode - 1) << 8, 0, option, n])
    mode, n = P.mval_val(a.req("value"), "value", modes=3)
    return Command(code=3038, params=[mode << 8, 1, option, n])


# --------------------------------------------------------------------------
# Script (3030, 3031 per further line)
# --------------------------------------------------------------------------
#
#     maniac.script("v[1] = 123;", "s[2] = 0;", results=[switches[4], variables[4]])
#     maniac.script(strings[3])
#
# p0 where the code is: 0 the text of the command and its 3031 lines, 1 strings[p1],
# 2 strings[variables[p1]]; p2, p3 0; p4 how many results, then (kind, id) for each:
# 0 switches[n], 1 switches[variables[n]], 2 variables[n], 3 variables[variables[n]],
# 4 strings[n], 5 strings[variables[n]].

SCRIPT_RESULTS = ["switches", "switches_ref", "variables", "variables_ref", "strings", "strings_ref"]


def script_src(head: Command, lines: list[str], ctx: Ctx) -> list[str] | None:
    """The arguments of maniac.script(...) for a Script command and its 3031 lines' texts."""
    from . import maniac_ext as X
    from . import pyexpr as P

    p = head.params
    if len(p) < 5 or p[2] or p[3] or p[4] < 0 or len(p) != 5 + 2 * p[4] or p[0] not in (0, 1, 2):
        return None
    if p[0] == 0:
        if p[1]:
            return None
        args = [pystr(ctx.dec(head.string))] + [pystr(line) for line in lines]
    else:
        if lines:
            return None
        src = X.text_src(p[0], p[1], ctx.dec(head.string))
        if src is None:
            return None
        args = [src]
    results = []
    for kind, n in zip(p[5::2], p[6::2]):
        if not 0 <= kind < 6:
            return None
        coll = ("switches", "variables", "strings")[kind // 2]
        one = {"switches": P.sw_src, "variables": P.var_src, "strings": P.str_src}[coll]
        results.append(one(n) if kind % 2 == 0 else "%s[%s]" % (coll, P.var_src(n)))
    if results:
        args.append("results=[%s]" % ", ".join(results))
    return args


def encode_script(call: Call, ctx: Ctx) -> list[Command]:
    """maniac.script(...): the Script command and one 3031 command per further line."""
    from . import maniac_ext as X
    from . import pyexpr as P

    results = call.kwargs.get("results", [])
    if set(call.kwargs) - {"results"} or not isinstance(results, list):
        raise CompileError("maniac.script() takes the code's lines, then results=[...]", call.node)
    params = []
    for r in results:
        kind = None
        for k, coll in enumerate(("switches", "variables", "strings")):
            if P.is_ref(r, coll):
                if P.is_int(r.index) or isinstance(r.index, str):
                    kind = 2 * k
                    n = r.index
                elif P.var_index(r.index) is not None:
                    kind, n = 2 * k + 1, r.index.index
        if kind is None:
            raise CompileError(
                "maniac.script(): results are switches[], variables[] or strings[] (id or variables[id])", call.node
            )
        params += [kind, n]
    head = [0, 0, 0, 0, len(results)]
    if len(call.args) == 1 and not isinstance(call.args[0], str):
        mode, n, _ = X.text_val(call.args[0], "maniac.script(): the code", call.node)
        return [Command(code=3030, params=[mode, n, 0, 0, len(results)] + params)]
    if not call.args or not all(isinstance(a, str) for a in call.args):
        raise CompileError("maniac.script(): the code is one string per line, or strings[id]", call.node)
    out = [Command(code=3030, params=head + params, string=ctx.enc(call.args[0], call.node))]
    out += [Command(code=3031, string=ctx.enc(line, call.node)) for line in call.args[1:]]
    return out


# --------------------------------------------------------------------------
# Get Game Info (3021)
# --------------------------------------------------------------------------
#
#     maniac.get_game_info("map_size", into=variables[1])     # width, height into 1, 2
#     maniac.get_game_info("tiles", layer="upper", x=0, y=0, width=20, height=15, into=variables[100])
#     maniac.get_game_info("bgm", name=strings[1], into=variables[1])
#
# p1 what: 0 map size, 1 tile ids, 2 window size, 3 screen pixels, 4 interpreter state,
# 5 tileset id, 6 a hero's face, 7 a hero's sprite, 8 camera position, 9 screen shake,
# 10 current music; then its arguments (tile ids: p0 selectors of x, y, width, height in
# bits 4-19, p2 layer, p3-p6 the area, p7 the first variable).  Pixels, interpreter, face
# and sprite keep the raw form.

GAME_INFOS = {0: "map_size", 2: "window_size", 5: "tileset", 8: "camera", 9: "screen_shake"}


def _decode_game_info(c: Command, ctx: Ctx) -> str | None:
    from . import pyexpr as P

    p = c.params
    if c.string or len(p) < 3:
        return None
    if p[1] in GAME_INFOS and len(p) == 3 and p[0] == 0:
        return "%s.get_game_info(%s, into=%s)" % (NAMESPACE, pystr(GAME_INFOS[p[1]]), P.var_src(p[2]))
    if p[1] == 10 and len(p) == 4 and p[0] == 0:
        return '%s.get_game_info("bgm", name=%s, into=%s)' % (NAMESPACE, P.str_src(p[2]), P.var_src(p[3]))
    if p[1] == 1 and len(p) == 8 and p[0] & ~0xFFFF0 == 0 and p[2] in (0, 1):
        values = [P.mval_src((p[0] >> 4 * (k + 1)) & 0xF, v) for k, v in enumerate(p[3:7])]
        if any(v is None or (p[0] >> 4 * (k + 1)) & 0xF > 2 for k, v in enumerate(values)):
            return None
        kw = ["layer=%s" % pystr(("lower", "upper")[p[2]])]
        kw += ["%s=%s" % kv for kv in zip(("x", "y", "width", "height"), values)]
        return '%s.get_game_info("tiles", %s, into=%s)' % (NAMESPACE, ", ".join(kw), P.var_src(p[7]))
    return None


def _encode_game_info(call: Call, ctx: Ctx) -> Command | None:
    from . import pyexpr as P

    if not call.args or not isinstance(call.args[0], str):
        return None  # the raw form: maniac.get_game_info(0, 8, 1)
    info = call.args[0]
    names = {**{n: k for k, n in GAME_INFOS.items()}, "bgm": 10, "tiles": 1}
    if info not in names:
        raise CompileError("get_game_info(): what is one of %s" % ", ".join(sorted(names)), call.node)
    keys = (
        ["into"]
        + (["name"] if info == "bgm" else [])
        + (["layer", "x", "y", "width", "height"] if info == "tiles" else [])
    )
    a = K.Args(Call(name=call.name, args=call.args[1:], kwargs=call.kwargs, node=call.node), keys)
    into = P.var_index(a.req("into"))
    if into is None:
        raise CompileError("get_game_info(): into is variables[id]", call.node)
    if info == "bgm":
        name = a.req("name")
        if not (P.is_ref(name, "strings") and P.is_int(name.index)):
            raise CompileError("get_game_info(): name is strings[id]", call.node)
        return Command(code=3021, params=[0, 10, name.index, into])
    if info != "tiles":
        return Command(code=3021, params=[0, names[info], into])
    modes, values = 0, []
    for k, key in enumerate(("x", "y", "width", "height")):
        mode, v = P.mval_val(a.get(key, 0), key, modes=3)
        modes |= mode << 4 * (k + 1)
        values.append(v)
    return Command(code=3021, params=[modes, 1, a.enum("layer", ["lower", "upper"], "lower")] + values + [into])


FORM_ENCODERS = {
    "get_game_info": _encode_game_info,
    "console_open": _encode_console,
    "console_close": _encode_console,
    "console_print": _encode_console,
    "console_input": _encode_console,
    "console_color": _encode_console,
    "get_media_option": _encode_media,
    "set_media_option": _encode_media,
}


# --------------------------------------------------------------------------
# Control Strings (3020): string variables as Python statements
# --------------------------------------------------------------------------
#
#     strings[1] = "orange"                        Assign
#     strings.name += strings[3].rjust(8)          Concatenate (padded to 8 characters)
#     strings[2] = str(variables.gold).rjust(6, "0")
#     strings[3] = db_name("item", variables.item)
#     variables.n = int(strings[1])                To number (int(strings[1], 16): hexadecimal)
#     variables.n = len(strings[1])                Length
#     variables.at = strings[1].find("sit")        Position of a text (-1: none)
#     variables.n = strings[1].split(", ", into=strings[10])
#     strings[2] = strings[1].pop_line()           First line, removed from strings[1]
#     strings[1].to_file("save.txt", "utf8")
#
# p0: 4 bits for the target (strings[a], strings[a:b+1], strings[variables[a]],
# strings[variables[a]:variables[b] + 1]), then 4 bits per argument (how it is given);
# p1, p2: the target; p3: operation, sub-operation << 8, flags << 16; p4-p6: arguments.

DB_TABLES = [
    "actor",
    "skill",
    "item",
    "enemy",
    "troop",
    "terrain",
    "attribute",
    "state",
    "animation",
    "tileset",
    "switch",
    "variable",
    "string",
    "common_event",
    "class",
    "battler_animation",
    "map",
    "event",
    "member",
]
ENCODINGS = ["ansi", "utf8"]
_HEX = 2  # flags: read the text as hexadecimal (int(strings[1], 16))


def _text_src(mode: int, arg: int, text: str, length: bool) -> str | None:
    """A text argument: the command's string (`length`: arg is its length), strings[arg]
    or strings[variables[arg]]."""
    from . import pyexpr as P

    if mode == 0:
        return pystr(text) if not length or arg == len(text) else None
    if text:
        return None
    if mode == 1:
        return P.str_src(arg)
    if mode == 2:
        return "strings[%s]" % P.var_src(arg)
    return None


def _strings_target_src(mode: int, a: int, b: int) -> str | None:
    from . import pyexpr as P

    if mode == 0 and b == 0:
        return P.str_src(a)
    if mode == 1:
        return P.target_src("strings", mode, a, b)
    if mode == 2 and b == 0:
        return "strings[%s]" % P.var_src(a)
    if mode == 3:
        return "strings[%s:%s + 1]" % (P.var_src(a), P.var_src(b))
    return None


def decode_strings(c: Command, ctx: Ctx) -> str | None:
    """Control Strings as a statement, None for one this form doesn't cover."""
    from . import pyexpr as P

    p = c.params
    if len(p) != 7 or p[0] < 0 or p[0] >> 20 or p[3] < 0 or p[3] >> 24:
        return None
    m = [(p[0] >> 4 * k) & 15 for k in range(1, 5)]
    op, fn, flags = p[3] & 255, (p[3] >> 8) & 255, p[3] >> 16
    a = p[4:7]
    text = ctx.dec(c.string)
    target = _strings_target_src(p[0] & 15, p[1], p[2])
    if target is None or m[3]:
        return None
    if op in (0, 1) and flags == 0:
        value = _string_value_src(fn, m, a, text)
        return None if value is None else "%s %s %s" % (target, ("=", "+=")[op], value)
    single = p[0] & 15 in (0, 2)
    if op in (2, 3) and single and not text and m[:3] == [0, 0, 0] and a[1:] == [0, 0]:
        if op == 3 and flags:
            return None
        if flags not in (0, _HEX):
            return None
        base = ", 16" if flags == _HEX else ""
        return "%s = %s(%s%s)" % (P.var_src(a[0]), ("int", "len")[op - 2], target, base)
    if flags or not single:
        return None
    if op == 4 and m[1] == 0:  # find: the variable gets the position
        what = _text_src(m[0], a[0], text, False)
        begin = P.mval_src(m[2], a[2])
        if what is None or begin is None or (m[0] == 0 and a[0] != 0):
            return None
        args = what if begin == "0" else "%s, %s" % (what, begin)
        return "%s = %s.find(%s)" % (P.var_src(a[1]), target, args)
    if op == 5 and m[2] == 0:  # split: the pieces from strings[a1], their number in variables[a2]
        what = _text_src(m[0], a[0], text, False)
        into = _strings_ref_src(m[1], a[1])
        if what is None or into is None or (m[0] == 0 and a[0] != 0):
            return None
        call = "%s.split(%s, into=%s)" % (target, what, into)
        return call if a[2] == 0 else "%s = %s" % (P.var_src(a[2]), call)
    if op == 7 and m[1] == 0 and m[2] == 0 and a[2] == 0 and 0 <= a[1] < len(ENCODINGS):
        name = _text_src(m[0], a[0], text, True)
        return None if name is None else "%s.to_file(%s, %s)" % (target, name, pystr(ENCODINGS[a[1]]))
    if op == 8 and m == [0, 0, 0, 0] and a[1:] == [0, 0] and not text:
        return "%s = %s.pop_line()" % (P.str_src(a[0]), target)
    return None


def _strings_ref_src(mode: int, arg: int) -> str | None:
    """strings[n] for an id given as a number, variables[n] or variables[variables[n]]."""
    from . import pyexpr as P

    if mode == 0:
        return P.str_src(arg)
    idx = P.mval_src(mode, arg)
    return None if idx is None or mode > 2 else "strings[%s]" % idx


def _string_value_src(fn: int, m: list[int], a: list[int], text: str) -> str | None:
    from . import pyexpr as P

    if fn == 0:  # a text, at least a[1] characters (spaces before)
        value = _text_src(m[0], a[0], text, True)
        if value is None or m[2] or a[2]:
            return None
        if m[1] == 0 and a[1] == 0:
            return value
        width = P.mval_src(m[1], a[1])
        return None if width is None else "%s.rjust(%s)" % (value, width)
    if fn in (1, 2):  # a number / a switch (ON or OFF), at least a[1] characters
        if text or m[2] or a[2]:
            return None
        if fn == 1:
            value = P.mval_src(m[0], a[0])
        else:
            value = P.sw_src(a[0]) if m[0] == 0 else "switches[%s]" % P.var_src(a[0]) if m[0] == 1 else None
        if value is None:
            return None
        if m[1] == 0 and a[1] == 0:
            return "str(%s)" % value
        width = P.mval_src(m[1], a[1])
        return None if width is None else "str(%s).rjust(%s%s)" % (value, width, ', "0"' if fn == 1 else "")
    if fn in (3, 4):  # the name / description of a database entry
        if text or m[0] or m[2] or not 0 <= a[0] < len(DB_TABLES) or a[2] not in (0, 1):
            return None
        ident = P.mval_src(m[1], a[1])
        if ident is None:
            return None
        current = ", current=True" if a[2] else ""
        return "%s(%s, %s%s)" % (("db_name", "db_description")[fn - 3], pystr(DB_TABLES[a[0]]), ident, current)
    if fn == 8:  # base.replace(search, replacement): the literal texts one after the other in the string
        pos = 0
        parts = []
        for k in range(3):
            if m[k] == 0:
                piece = text[pos : pos + a[k]]
                if a[k] < 0 or len(piece) != a[k]:
                    return None
                parts.append(pystr(piece))
                pos += a[k]
            else:
                parts.append(_text_src(m[k], a[k], "", False))
        if pos != len(text) or None in parts:
            return None
        return "%s.replace(%s, %s)" % tuple(parts)
    if fn == 12:  # a file's text
        name = _text_src(m[0], a[0], text, True)
        if name is None or m[1] or m[2] or a[2] or not 0 <= a[1] < len(ENCODINGS):
            return None
        return "read_file(%s, %s)" % (name, pystr(ENCODINGS[a[1]]))
    return None


class _Strings(BaseModel):
    """A Control Strings command being built."""

    p0: int = 0
    a: list[int] = [0, 0, 0]
    text: str = ""

    def arg(self, k: int, mode: int, value: int) -> None:
        self.p0 |= mode << 4 * (k + 1)
        self.a[k] = value

    def text_arg(self, k: int, v, length: bool, what: str) -> None:
        from . import pyexpr as P

        if isinstance(v, str):
            self.text = v
            self.arg(k, 0, len(v) if length else 0)
        elif P.is_ref(v, "strings") and P.is_int(v.index):
            self.arg(k, 1, v.index)
        elif P.is_ref(v, "strings") and P.var_index(v.index) is not None:
            self.arg(k, 2, v.index.index)
        else:
            raise P._err('%s is a text: "...", strings[id] or strings[variables[id]] (got %s)' % (what, P.show(v)), v)

    def texts(self, values: list, what: str) -> None:
        """Texts in arguments 0-2, the literal ones one after the other in the string."""
        from . import pyexpr as P

        for k, v in enumerate(values):
            if isinstance(v, str):
                self.arg(k, 0, len(v))
                self.text += v
            else:
                self.text_arg(k, v, False, what)
        if not all(isinstance(v, str) or P.is_ref(v, "strings") for v in values):
            raise P._err("%s takes texts" % what, values[0])

    def command(
        self, target: tuple[int, int, int], op: int, fn: int = 0, flags: int = 0, ctx: Ctx | None = None, node=None
    ) -> Command:
        mode, a, b = target
        string = ctx.enc(self.text, node) if ctx is not None else b""
        return Command(code=3020, params=[self.p0 | mode, a, b, op | fn << 8 | flags << 16] + self.a, string=string)


def _strings_target_val(v) -> tuple[int, int, int]:
    from . import pyexpr as P

    mode, a, b = P.target_val(v)
    return mode, a, 0 if mode in (0, 2) else b


def _width(s: _Strings, call, padding: str) -> None:
    """The width of `.rjust(width[, "0"])`, into argument 1."""
    from . import pyexpr as P

    args = K.Args(call, ["width", "fillchar"])
    fill = args.get("fillchar", " ")
    if fill != padding:
        raise P._err(
            "rjust(): this pads with %r" % padding if padding == " " else 'str(number).rjust(width, "0")', call
        )
    s.arg(1, *P.mval_val(args.req("width"), "rjust(): width", 3))


def encode_string_value(target, op: int, v, ctx: Ctx, node) -> Command:
    """`strings[...] = value` (op 0) / `+= value` (op 1)."""
    from . import pyexpr as P

    t = _strings_target_val(target)
    s = _Strings()
    padded = isinstance(v, Call) and v.name == "rjust" and v.target is not None
    inner = v.target if padded else v
    if isinstance(inner, Call) and inner.target is None and inner.name == "str":
        x = K.Args(inner, ["value"]).req("value")
        if P.is_ref(x, "switches"):
            if P.int_index(x, "switches") is not None:
                s.arg(0, 0, x.index)
            elif P.var_index(x.index) is not None:
                s.arg(0, 1, x.index.index)
            else:
                raise P._err("str(): switches[id] or switches[variables[id]]", x)
            fn = 2
        else:
            s.arg(0, *P.mval_val(x, "str(): value"))
            fn = 1
        if padded:
            _width(s, v, "0" if fn == 1 else " ")
        return s.command(t, op, fn, ctx=ctx, node=node)
    if padded or not isinstance(inner, Call) or inner.name == "__call__":
        s.text_arg(0, inner, True, "the value")
        if padded:
            _width(s, v, " ")
        return s.command(t, op, 0, ctx=ctx, node=node)
    if inner.name == "replace" and inner.target is not None:
        args = K.Args(inner, ["old", "new"])
        s.texts([inner.target, args.req("old"), args.req("new")], "replace()")
        return s.command(t, op, 8, ctx=ctx, node=node)
    if inner.target is None and inner.name in ("db_name", "db_description"):
        args = K.Args(inner, ["table", "id", "current"])
        table = args.get("table")
        if table not in DB_TABLES:
            raise P._err("%s(): table is one of %s" % (inner.name, ", ".join(DB_TABLES)), inner)
        s.arg(0, 0, DB_TABLES.index(table))
        s.arg(1, *P.mval_val(args.req("id"), "%s(): id" % inner.name, 3))
        s.arg(2, 0, int(args.bool("current")))
        return s.command(t, op, 3 if inner.name == "db_name" else 4, ctx=ctx, node=node)
    if inner.target is None and inner.name == "read_file":
        args = K.Args(inner, ["name", "encoding"])
        s.text_arg(0, args.req("name"), True, "read_file(): name")
        s.arg(1, 0, args.enum("encoding", ENCODINGS, "ansi"))
        return s.command(t, op, 12, ctx=ctx, node=node)
    if inner.name == "pop_line" and op == 0 and P.is_ref(inner.target, "strings"):
        K.Args(inner, [])
        out = P.int_index(target, "strings")
        if out is None:
            raise P._err("pop_line() puts the line into one strings[id]", inner)
        s.arg(0, 0, out)
        return s.command(_strings_target_val(inner.target), 8, ctx=ctx, node=node)
    raise P._err(
        'a string variable takes a text ("...", strings[id]), str(number), str(switches[id]), '
        "db_name(...), db_description(...), read_file(...) or strings[id].pop_line() (got %s)" % P.show(v),
        v,
    )


def encode_string_query(var: int, v, ctx: Ctx) -> Command | None:
    """`variables[n] = int(strings[1])` / len(...) / strings[1].find(...) / .split(...), None
    when the value is no such query."""
    from . import pyexpr as P

    if not isinstance(v, Call):
        return None
    s = _Strings()
    if v.target is None and v.name in ("int", "len"):
        args = K.Args(v, ["text", "base"] if v.name == "int" else ["text"])
        x = args.req("text")
        if not P.is_ref(x, "strings"):
            return None
        base = args.get("base", 10)
        if base not in (10, 16):
            raise P._err("int(): base 10 or 16", v)
        s.arg(0, 0, var)
        return s.command(_strings_target_val(x), 2 if v.name == "int" else 3, flags=_HEX if base == 16 else 0)
    if not P.is_ref(v.target, "strings"):
        return None
    if v.name == "find":
        args = K.Args(v, ["text", "start"])
        s.text_arg(0, args.req("text"), False, "find(): text")
        s.arg(1, 0, var)
        s.arg(2, *P.mval_val(args.get("start", 0), "find(): start", 3))
        return s.command(_strings_target_val(v.target), 4, ctx=ctx, node=v.node)
    if v.name == "split":
        return _split(v, var, ctx)
    return None


def _split(v: Call, var: int, ctx: Ctx) -> Command:
    from . import pyexpr as P

    args = K.Args(v, ["separator", "into"])
    s = _Strings()
    s.text_arg(0, args.req("separator"), False, "split(): separator")
    into = args.req("into")
    if not P.is_ref(into, "strings") or isinstance(into.index, P.Range):
        raise P._err("split(): into is strings[id], where the pieces start", v)
    s.arg(1, *P.mval_val(into.index, "split(): into", 3))
    s.arg(2, 0, var)
    return s.command(_strings_target_val(v.target), 5, ctx=ctx, node=v.node)


def encode_string_call(call: Call, ctx: Ctx) -> Command | None:
    """`strings[1].split(...)` (without counting) / `strings[1].to_file(...)` as statements."""
    from . import pyexpr as P

    if not P.is_ref(call.target, "strings"):
        return None
    if call.name == "split":
        return _split(call, 0, ctx)
    if call.name == "to_file":
        args = K.Args(call, ["name", "encoding"])
        s = _Strings()
        s.text_arg(0, args.req("name"), True, "to_file(): name")
        s.arg(1, 0, args.enum("encoding", ENCODINGS, "ansi"))
        return s.command(_strings_target_val(call.target), 7, ctx=ctx, node=call.node)
    raise P._err("string variables have .find(), .split(), .rjust(), .pop_line() and .to_file()", call)
