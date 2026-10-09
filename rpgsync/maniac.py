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
        min_params=0,
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

named.register(SPECS)
