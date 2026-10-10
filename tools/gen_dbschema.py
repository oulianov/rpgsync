"""Generate rpgsync/dbschema.py and rpgsync/db.py from liblcf's schema CSVs.

    uv run python tools/gen_dbschema.py [--csv-dir PATH] [--out-dir PATH]

liblcf (https://github.com/EasyRPG/liblcf) describes every LCF structure in
``generator/csv``: ``fields.csv`` (+ ``fields_easyrpg.csv``), ``structs.csv``,
``enums.csv``, ``flags.csv`` and ``constants.csv``.  This script turns the
database (``ldb``) part of it into

* ``rpgsync/dbschema.py``: the raw schema as data (chunk id, name, liblcf type,
  codec, default, PersistIfDefault, Is2k3, size chunk, description) plus the
  enums, flags and the list of exported database tables;
* ``rpgsync/db.py``: one pydantic model per structure used by the exported
  tables, with liblcf defaults and a description per field.

The CSVs are not shipped with rpgsync; the generated files are committed.
"""

from __future__ import annotations

import csv
import os
import re
from typing import Any

import typer
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(HERE, "..", "..", "Player", "lib", "liblcf", "generator", "csv")
DEFAULT_OUT = os.path.join(HERE, "..", "rpgsync")

# Exported database tables: name -> (Database chunk id, structure, title).
TABLES: list[tuple[str, int, str, str]] = [
    ("actors", 0x0B, "Actor", "Actors"),
    ("classes", 0x1E, "Class", "Classes (RPG Maker 2003)"),
    ("skills", 0x0C, "Skill", "Skills"),
    ("items", 0x0D, "Item", "Items"),
    ("enemies", 0x0E, "Enemy", "Enemies"),
    ("troops", 0x0F, "Troop", "Troops"),
    ("states", 0x12, "State", "States"),
    ("attributes", 0x11, "Attribute", "Attributes"),
    ("terrains", 0x10, "Terrain", "Terrains"),
    ("battleranimations", 0x20, "BattlerAnimation", "Battler animations (RPG Maker 2003)"),
    ("switches", 0x17, "Switch", "Switches"),
    ("variables", 0x18, "Variable", "Variables"),
]

# Fields kept opaque (never exported, written back untouched).
OPAQUE = {("Troop", "pages")}

CLASS_DOCS = {
    "Actor": "A hero (Database > Actors).",
    "Class": "A character class (Database > Classes, RPG Maker 2003).",
    "Skill": "A skill (Database > Skills).",
    "Item": "An item, weapon or armor (Database > Items).",
    "Enemy": "A monster (Database > Enemies).",
    "EnemyAction": "One entry of a monster's action pattern.",
    "Troop": "A monster party (Database > Troops).\n\n"
    "    The troop's battle events (pages) are in database/troop_events.py.",
    "TroopMember": "A monster placed in a troop.",
    "State": "A condition such as poison or death (Database > States).",
    "Attribute": "An attribute / element (Database > Attributes).",
    "Terrain": "A terrain type (Database > Terrain).",
    "BattlerAnimation": "A battle character set: poses and weapons of a 2003 battler (Database > Battle Animation 2).",
    "BattlerAnimationPose": "One pose of a battler animation.",
    "BattlerAnimationWeapon": "One weapon graphic of a battler animation.",
    "BattlerAnimationItemSkill": "How one actor animates when using a weapon or skill (RPG Maker 2003).",
    "Switch": "A switch name.",
    "Variable": "A variable name.",
    "Learning": "A skill learned at a given level.",
    "Sound": "A sound effect.",
    "Music": "A music track.",
    "Parameters": "Base stats per level: element N-1 of each list is the value at level N.",
    "Equipment": "Initial equipment: item ids, 0 = nothing.",
}

# Better descriptions for fields whose liblcf comment only states the type.
FIELD_DOCS = {
    ("Actor", "title"): "Title shown under the name (e.g. 'Knight')",
    ("Actor", "character_name"): "CharSet file used on the map",
    ("Actor", "character_index"): "Sprite index (0-7) inside the CharSet",
    ("Actor", "transparent"): "Draw the map sprite translucent",
    ("Actor", "initial_level"): "Level when joining the party",
    ("Actor", "final_level"): "Maximum level",
    ("Actor", "critical_hit"): "Can land critical hits",
    ("Actor", "critical_hit_chance"): "Critical hit probability: 1 in N",
    ("Actor", "face_name"): "FaceSet file",
    ("Actor", "face_index"): "Face index (0-15) inside the FaceSet",
    ("Actor", "two_weapon"): "Wields two weapons instead of weapon + shield",
    ("Actor", "lock_equipment"): "Equipment cannot be changed",
    ("Actor", "auto_battle"): "Fights automatically",
    ("Actor", "super_guard"): "Strong defense (damage quartered when defending)",
    ("Actor", "parameters"): "Max HP/SP, attack, defense, spirit and agility per level",
    ("Actor", "exp_base"): "Experience curve: base value",
    ("Actor", "exp_inflation"): "Experience curve: inflation",
    ("Actor", "exp_correction"): "Experience curve: correction",
    ("Actor", "initial_equipment"): "Equipment when joining the party",
    ("Actor", "unarmed_animation"): "Battle animation of unarmed attacks",
    ("Actor", "class_id"): "Class",
    ("Actor", "battle_x"): "Battle position X",
    ("Actor", "battle_y"): "Battle position Y",
    ("Actor", "battler_animation"): "Battler animation (battle charset)",
    ("Actor", "skills"): "Skills learned by level",
    ("Actor", "rename_skill"): "Use a custom name for the Skill battle command",
    ("Actor", "skill_name"): "Custom name of the Skill battle command",
    ("Actor", "battle_commands"): "Battle command ids (-1 = none)",
    ("Class", "battler_animation"): "Battler animation (battle charset)",
    ("Class", "battle_commands"): "Battle command ids (-1 = none)",
    ("Class", "skills"): "Skills learned by level",
    ("Class", "parameters"): "Max HP/SP, attack, defense, spirit and agility per level",
    ("Learning", "level"): "Level at which the skill is learned",
    ("Learning", "skill_id"): "Learned skill",
    ("Skill", "description"): "Help text",
    ("Skill", "using_message1"): "Battle message when used (first line)",
    ("Skill", "using_message2"): "Battle message when used (second line)",
    ("Skill", "failure_message"): "Message shown when the skill fails",
    ("Skill", "sp_type"): "SP cost unit",
    ("Skill", "sp_percent"): "SP cost in percent of max SP",
    ("Skill", "sp_cost"): "SP cost",
    ("Skill", "scope"): "Target",
    ("Skill", "switch_id"): "Switch turned ON by switch skills",
    ("Skill", "animation_id"): "Battle animation",
    ("Skill", "sound_effect"): "Sound of teleport/escape/switch skills",
    ("Skill", "occasion_field"): "Usable on the map",
    ("Skill", "occasion_battle"): "Usable in battle",
    ("Skill", "reverse_state_effect"): "Inflict the states instead of curing them",
    ("Skill", "physical_rate"): "Attack influence (0-10)",
    ("Skill", "magical_rate"): "Spirit influence (0-10)",
    ("Skill", "variance"): "Damage variance (0-10)",
    ("Skill", "power"): "Base effect value",
    ("Skill", "hit"): "Success rate in percent",
    ("Skill", "absorb_damage"): "Absorb the damage",
    ("Skill", "ignore_defense"): "Ignore the target's defense",
    ("Skill", "state_effects"): "States changed by the skill",
    ("Skill", "attribute_effects"): "Attributes of the skill",
    ("Skill", "affect_attr_defence"): "Lower the target's resistance to the attributes",
    ("Skill", "battler_animation"): "Battler animation used",
    ("Skill", "battler_animation_data"): "Battle animation settings per actor",
    ("Item", "description"): "Help text",
    ("Item", "price"): "Shop price",
    ("Item", "uses"): "Number of uses (0 = unlimited)",
    ("Item", "atk_points1"): "Attack bonus (equipment)",
    ("Item", "def_points1"): "Defense bonus (equipment)",
    ("Item", "spi_points1"): "Spirit bonus (equipment)",
    ("Item", "agi_points1"): "Agility bonus (equipment)",
    ("Item", "two_handed"): "Two-handed weapon",
    ("Item", "sp_cost"): "SP consumed per attack",
    ("Item", "hit"): "Hit rate in percent",
    ("Item", "critical_hit"): "Critical hit bonus in percent",
    ("Item", "animation_id"): "Battle animation",
    ("Item", "preemptive"): "Attack first",
    ("Item", "dual_attack"): "Attack twice",
    ("Item", "attack_all"): "Attack all enemies",
    ("Item", "ignore_evasion"): "Ignore evasion",
    ("Item", "prevent_critical"): "Prevent critical hits",
    ("Item", "raise_evasion"): "Raise evasion",
    ("Item", "half_sp_cost"): "Halve SP costs",
    ("Item", "no_terrain_damage"): "No terrain damage",
    ("Item", "cursed"): "Cursed (cannot be unequipped)",
    ("Item", "entire_party"): "Medicine affects the entire party",
    ("Item", "recover_hp_rate"): "HP recovered in percent",
    ("Item", "recover_hp"): "HP recovered",
    ("Item", "recover_sp_rate"): "SP recovered in percent",
    ("Item", "recover_sp"): "SP recovered",
    ("Item", "occasion_field1"): "Medicine usable on the map only",
    ("Item", "ko_only"): "Only usable on knocked out actors",
    ("Item", "max_hp_points"): "Max HP increase (books/material)",
    ("Item", "max_sp_points"): "Max SP increase (books/material)",
    ("Item", "atk_points2"): "Attack increase (books/material)",
    ("Item", "def_points2"): "Defense increase (books/material)",
    ("Item", "spi_points2"): "Spirit increase (books/material)",
    ("Item", "agi_points2"): "Agility increase (books/material)",
    ("Item", "using_message"): "Message shown when used (0 = default)",
    ("Item", "skill_id"): "Skill invoked by special items",
    ("Item", "switch_id"): "Switch turned ON by switch items",
    ("Item", "occasion_field2"): "Switch item usable on the map",
    ("Item", "occasion_battle"): "Switch item usable in battle",
    ("Item", "actor_set"): "Actors who can use/equip the item",
    ("Item", "state_set"): "States changed by the item",
    ("Item", "attribute_set"): "Attributes of the item",
    ("Item", "state_chance"): "State change probability in percent",
    ("Item", "reverse_state_effect"): "Inflict the states instead of curing them",
    ("Item", "weapon_animation"): "Weapon animation",
    ("Item", "animation_data"): "Battle animation settings per actor",
    ("Item", "use_skill"): "Weapon/armor invokes its skill when used",
    ("Item", "class_set"): "Classes who can use/equip the item",
    ("Item", "ranged_trajectory"): "Trajectory of ranged weapons",
    ("Item", "ranged_target"): "Target of ranged weapons",
    ("Enemy", "battler_name"): "Monster graphic",
    ("Enemy", "battler_hue"): "Hue shift of the graphic",
    ("Enemy", "transparent"): "Draw translucent",
    ("Enemy", "exp"): "Experience given",
    ("Enemy", "gold"): "Gold dropped",
    ("Enemy", "drop_id"): "Dropped item (0 = none)",
    ("Enemy", "drop_prob"): "Drop probability in percent",
    ("Enemy", "critical_hit"): "Can land critical hits",
    ("Enemy", "critical_hit_chance"): "Critical hit probability: 1 in N",
    ("Enemy", "miss"): "Attacks often miss",
    ("Enemy", "levitate"): "Flying (floats up and down)",
    ("Enemy", "actions"): "Action pattern",
    ("EnemyAction", "kind"): "Action type",
    ("EnemyAction", "basic"): "Basic action (kind='basic')",
    ("EnemyAction", "skill_id"): "Skill (kind='skill')",
    ("EnemyAction", "enemy_id"): "Monster to transform into (kind='transformation')",
    ("EnemyAction", "condition_type"): "Condition",
    ("EnemyAction", "condition_param1"): "Condition: first value (switch id, turn, %, level...)",
    ("EnemyAction", "condition_param2"): "Condition: second value",
    ("EnemyAction", "switch_id"): "Condition switch",
    ("EnemyAction", "switch_on"): "Turn a switch ON after the action",
    ("EnemyAction", "switch_on_id"): "Switch turned ON",
    ("EnemyAction", "switch_off"): "Turn a switch OFF after the action",
    ("EnemyAction", "switch_off_id"): "Switch turned OFF",
    ("EnemyAction", "rating"): "Priority (1-10 in the editor)",
    ("TroopMember", "enemy_id"): "Monster",
    ("TroopMember", "x"): "Screen position X",
    ("TroopMember", "y"): "Screen position Y",
    ("TroopMember", "invisible"): "Hidden until shown by an event",
    ("Troop", "members"): "Monsters of the troop",
    ("Troop", "auto_alignment"): "Align the monsters automatically",
    ("Troop", "terrain_set"): "Terrains where the troop can be encountered",
    ("Troop", "appear_randomly"): "Random encounter only on the selected terrains",
    ("Terrain", "damage"): "Damage per step",
    ("Terrain", "encounter_rate"): "Encounter rate multiplier in percent",
    ("Terrain", "background_name"): "Battle background",
    ("Terrain", "boat_pass"): "Small boat can pass",
    ("Terrain", "ship_pass"): "Ship can pass",
    ("Terrain", "airship_pass"): "Airship can pass",
    ("Terrain", "airship_land"): "Airship can land",
    ("Terrain", "bush_depth"): "How much of a character is hidden",
    ("Terrain", "footstep"): "Footstep sound",
    ("Terrain", "on_damage_se"): "Play the footstep sound only on damage",
    ("Terrain", "background_type"): "Battle background mode",
    ("Terrain", "special_flags"): "Enabled special battle situations",
    ("Terrain", "special_back_party"): "Back attack probability (party) in percent",
    ("Terrain", "special_back_enemies"): "Back attack probability (enemies) in percent",
    ("Terrain", "special_lateral_party"): "Pincer attack probability (party) in percent",
    ("Terrain", "special_lateral_enemies"): "Pincer attack probability (enemies) in percent",
    ("Terrain", "grid_location"): "Battle grid location",
    ("Terrain", "grid_top_y"): "Battle grid top Y",
    ("Terrain", "grid_elongation"): "Battle grid elongation",
    ("Terrain", "grid_inclination"): "Battle grid inclination",
    ("State", "type"): "Whether the state ends after battle",
    ("State", "color"): "Color of the state name",
    ("State", "priority"): "Display priority",
    ("State", "restriction"): "Action restriction",
    ("State", "a_rate"): "Infliction rate for rank A in percent",
    ("State", "b_rate"): "Infliction rate for rank B in percent",
    ("State", "c_rate"): "Infliction rate for rank C in percent",
    ("State", "d_rate"): "Infliction rate for rank D in percent",
    ("State", "e_rate"): "Infliction rate for rank E in percent",
    ("State", "hold_turn"): "Minimum duration in turns",
    ("State", "auto_release_prob"): "Recovery probability per turn after hold_turn",
    ("State", "release_by_damage"): "Recovery probability when damaged",
    ("State", "affect_type"): "How the stat changes apply",
    ("State", "affect_attack"): "Changes attack",
    ("State", "affect_defense"): "Changes defense",
    ("State", "affect_spirit"): "Changes spirit",
    ("State", "affect_agility"): "Changes agility",
    ("State", "reduce_hit_ratio"): "Hit ratio in percent",
    ("State", "avoid_attacks"): "Evade all physical attacks",
    ("State", "reflect_magic"): "Reflect magic",
    ("State", "cursed"): "Equipment cannot be changed",
    ("State", "battler_animation_id"): "Battler animation pose (100 = none)",
    ("State", "restrict_skill"): "Blocks physical skills",
    ("State", "restrict_skill_level"): "Physical skills blocked from this attack influence",
    ("State", "restrict_magic"): "Blocks magic skills",
    ("State", "restrict_magic_level"): "Magic skills blocked from this spirit influence",
    ("State", "hp_change_type"): "HP change direction",
    ("State", "sp_change_type"): "SP change direction",
    ("State", "message_actor"): "Message when an actor is affected",
    ("State", "message_enemy"): "Message when a monster is affected",
    ("State", "message_already"): "Message when already affected",
    ("State", "message_affected"): "Message each turn",
    ("State", "message_recovery"): "Message on recovery",
    ("State", "hp_change_max"): "HP change per turn in percent of max HP",
    ("State", "hp_change_val"): "HP change per turn",
    ("State", "hp_change_map_steps"): "HP change on the map: every N steps",
    ("State", "hp_change_map_val"): "HP change on the map",
    ("State", "sp_change_max"): "SP change per turn in percent of max SP",
    ("State", "sp_change_val"): "SP change per turn",
    ("State", "sp_change_map_steps"): "SP change on the map: every N steps",
    ("State", "sp_change_map_val"): "SP change on the map",
    ("Attribute", "type"): "Physical or magical",
    ("Attribute", "a_rate"): "Damage rate for rank A in percent",
    ("Attribute", "b_rate"): "Damage rate for rank B in percent",
    ("Attribute", "c_rate"): "Damage rate for rank C in percent",
    ("Attribute", "d_rate"): "Damage rate for rank D in percent",
    ("Attribute", "e_rate"): "Damage rate for rank E in percent",
    ("Sound", "name"): "File in Sound/ ('(OFF)' = none)",
    ("Sound", "volume"): "Volume in percent",
    ("Sound", "tempo"): "Tempo in percent",
    ("Sound", "balance"): "Balance (0 = left, 50 = center, 100 = right)",
    ("Music", "name"): "File in Music/ ('(OFF)' = none)",
    ("Music", "fadein"): "Fade in time in milliseconds",
    ("Music", "volume"): "Volume in percent",
    ("Music", "tempo"): "Tempo in percent",
    ("Music", "balance"): "Balance (0 = left, 50 = center, 100 = right)",
    ("Switch", "name"): "Name shown in the editor",
    ("Variable", "name"): "Name shown in the editor",
    ("BattlerAnimation", "speed"): "Animation speed",
    ("BattlerAnimation", "poses"): "Poses (Idle, AttackRight, AttackLeft, Skill, Dead, Damage, Dazed, "
    "Defend, WalkLeft, WalkRight, Victory, Item, ...)",
    ("BattlerAnimation", "weapons"): "Weapon graphics",
    ("BattlerAnimationPose", "battler_name"): "Battle CharSet file",
    ("BattlerAnimationPose", "battler_index"): "Index inside the file",
    ("BattlerAnimationPose", "animation_type"): "Battle CharSet or battle animation",
    ("BattlerAnimationPose", "battle_animation_id"): "Battle animation (animation_type='battle')",
    ("BattlerAnimationWeapon", "weapon_name"): "Weapon graphic file",
    ("BattlerAnimationWeapon", "weapon_index"): "Index inside the file",
}

STRUCT_KIND_TYPES = {"Parameters": "params", "Equipment": "equip"}


class Row(BaseModel):
    struct: str
    name: str
    is_size: bool
    type: str
    index: int | None
    default: str
    persist: bool
    is2k3: bool
    comment: str


def _read_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_"))


class Schema:
    def __init__(self, csv_dir: str):
        def both(name):
            rows = _read_csv(os.path.join(csv_dir, name + ".csv"))
            extra = os.path.join(csv_dir, name + "_easyrpg.csv")
            if os.path.exists(extra):
                rows += _read_csv(extra)
            return rows

        self.structs: dict[str, bool] = {}  # ldb struct -> has ID
        for r in both("structs"):
            if r["Type"] == "ldb":
                self.structs[r["Structure"]] = r["Index available?"] == "1"
        self.rows: dict[str, list[Row]] = {s: [] for s in self.structs}
        for r in both("fields"):
            if r["Structure"] not in self.structs:
                continue
            self.rows[r["Structure"]].append(
                Row(
                    struct=r["Structure"],
                    name=r["Field"],
                    is_size=r["Size Field?"] == "t",
                    type=r["Type"],
                    index=int(r["Index"], 16) if r["Index"] else None,
                    default=r["Default Value"],
                    persist=r["PersistIfDefault"] == "1",
                    is2k3=r["Is2k3"] == "1",
                    comment=r["Comment"],
                )
            )
        for s in self.rows:
            # fields.csv lists fields in chunk order, easyrpg extensions come last
            self.rows[s].sort(key=lambda row: (row.index is None, row.index or 0))
        self.enums: dict[str, dict[str, int]] = {}
        for r in both("enums"):
            self.enums.setdefault(r["Structure"] + "_" + r["Entry"], {})[r["Value"]] = int(r["Index"])
        self.flags: dict[str, list[tuple[str, bool]]] = {}
        for r in both("flags"):
            self.flags.setdefault(r["Structure"] + "_Flags", []).append((r["Field"], r["Is2k3"] == "1"))
        self.constants: dict[str, str] = {}
        for r in _read_csv(os.path.join(csv_dir, "constants.csv")):
            self.constants[r["name"]] = r["value"]

    # -- type mapping ------------------------------------------------------------
    def inner(self, t: str) -> str:
        """'Array<BattlerAnimationItemSkill:Ref<Actor>>' -> 'BattlerAnimationItemSkill'."""
        t = t[t.index("<") + 1 : -1]
        return t.split(":")[0]

    def kinds(self, row: Row, closure: set | None = None) -> tuple[str, str]:
        """(LCF codec, model kind) of a field."""
        t = row.type
        if (row.struct, row.name) in OPAQUE:
            return "raw", "raw"
        if t in ("Int32", "UInt32") or t.startswith("Ref<"):
            return "int", "int"
        if t == "Boolean":
            return "bool", "bool"
        if t in ("DBString", "String"):
            return "str", "str"
        if t.startswith("Enum<"):
            return "int", "enum:" + t[5:-1]
        if t == "DBBitArray" or t == "Vector<Bool>":
            return "u8vec", "bits"
        if t == "Vector<UInt8>":
            return "u8vec", "u8"
        if t == "Vector<Int16>":
            return "i16vec", "i16"
        if t in ("Vector<Int32>", "Vector<UInt32>") or re.fullmatch(r"Vector<Ref<\w+:Int32>>", t):
            return "i32vec", "i32"
        if t in STRUCT_KIND_TYPES:
            return "i16vec", STRUCT_KIND_TYPES[t]
        if t in self.flags:
            return "u8vec", "flags:" + t
        if t.startswith("Array<"):
            inner = self.inner(t)
            if inner in self.structs and (closure is None or inner in closure):
                return "A:" + inner, "array:" + inner
            return "raw", "raw"
        if t in self.structs and (closure is None or t in closure):
            return "S:" + t, "struct:" + t
        return "raw", "raw"

    def closure(self) -> list[str]:
        """Structures reachable from the exported tables, dependencies first."""
        order: list[str] = []

        def visit(s: str):
            if s in order:
                return
            for row in self.rows[s]:
                if row.is_size:
                    continue
                _, model = self.kinds(row)
                if model.startswith(("struct:", "array:")):
                    visit(model.split(":", 1)[1])
                elif model in ("params", "equip"):
                    visit(row.type)
            order.append(s)

        for _, _, s, _ in TABLES:
            visit(s)
        return order

    # -- defaults -----------------------------------------------------------------
    def default(self, row: Row, model: str) -> tuple[Any, Any]:
        """(RPG Maker 2000 default, RPG Maker 2003 default), model level."""
        raw = row.default.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        m = re.fullmatch(r"DBString\((\w+)\)", raw)
        if m:
            raw = self.constants[m.group(1)].strip('"')
        if "|" in raw:
            a, b = raw.split("|")
            return int(a), int(b)
        if model == "int":
            v = int(raw) if raw else 0
        elif model == "bool":
            v = raw in ("True", "1")
        elif model == "str":
            v = raw
        elif model.startswith("enum:"):
            n = int(raw) if raw else 0
            names = [k for k, x in self.enums[model[5:]].items() if x == n]
            v = names[0] if names else n
        elif model in ("bits", "u8", "i16", "i32", "array") or model.startswith("array:"):
            v = _list_literal(raw) if raw else []
        else:
            v = None
        return v, v


def _list_literal(src: str) -> list:
    """Evaluate a liblcf vector default such as ``[31]+[15]*143``."""
    import ast

    def ev(n):
        if isinstance(n, ast.List):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Mult)):
            a, b = ev(n.left), ev(n.right)
            return a + b if isinstance(n.op, ast.Add) else a * b
        raise SystemExit("unsupported vector default %r" % src)

    return ev(ast.parse(src, mode="eval").body)


# --------------------------------------------------------------------------
# Descriptions
# --------------------------------------------------------------------------

_TYPE_WORD = re.compile(
    r"^(Integer|String|Flag|Array|Short|Bitflag|Uint32|Array x 6|Integer x 5|\?"
    r"|rpg::\w+|x 2 if RPG2003|RPG2003|RPG2000)$"
)
_REF_TABLE = {
    "Actor": "actor",
    "Skill": "skill",
    "Item": "item",
    "Enemy": "monster",
    "Switch": "switch",
    "Variable": "variable",
    "Animation": "battle animation",
    "Class": "class",
    "BattlerAnimation": "battler animation",
    "BattleCommand": "battle command",
    "Terrain": "terrain",
    "CommonEvent": "common event",
    "Map": "map",
    "BattlerAnimationWeapon": "battler animation weapon",
    "BattlerAnimationPose": "battler animation pose",
}


def _set_target(name: str) -> str:
    for key, label in (
        ("state", "state"),
        ("attribute", "attribute"),
        ("actor", "actor"),
        ("class", "class"),
        ("terrain", "terrain"),
    ):
        if key in name:
            return label
    return "entry"


def describe(schema: Schema, row: Row, model: str) -> str:
    parts = [p.strip() for p in row.comment.split(" - ")]
    text = " - ".join(p for p in parts if p and not _TYPE_WORD.match(p))
    custom = FIELD_DOCS.get((row.struct, row.name))
    if custom:
        text = custom
    if not text:
        text = row.name.replace("_", " ")
        text = text[:1].upper() + text[1:]
    text = text.rstrip(".")
    notes = []
    m = re.fullmatch(r"Ref<(\w+)(:\w+)?>", row.type)
    if m and m.group(1) in _REF_TABLE:
        notes.append("%s id" % _REF_TABLE[m.group(1)])
    if model.startswith("enum:"):
        notes.append("one of " + ", ".join(repr(k) for k in schema.enums[model[5:]]) + " or a number")
    if model == "bits":
        notes.append("one flag per %s id, index 0 is id 1" % _set_target(row.name))
    if model == "u8" and "ranks" in row.name:
        notes.append("rank per %s id, index 0 is id 1: 0=A 1=B 2=C 3=D 4=E" % _set_target(row.name))
    out = text
    if notes and out.endswith(")"):
        out = out[:-1] + "; " + "; ".join(notes) + ")"
    elif notes:
        out += " (" + "; ".join(notes) + ")"
    out += "."
    if row.is2k3:
        out += " RPG Maker 2003 only."
    if "RPG2000" in parts:
        out += " RPG Maker 2000 only."
    if row.name.startswith("easyrpg_"):
        out += " EasyRPG Player extension."
    if row.name.startswith("maniac_"):
        out += " Maniac Patch extension."
    return out


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

HEADER = '''"""{doc}

Generated by tools/gen_dbschema.py from liblcf's generator/csv; do not edit.
"""
'''


def _pyrepr(v: Any) -> str:
    if isinstance(v, str):
        return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')
    return repr(v)


def gen_dbschema(schema: Schema) -> str:
    closure = set(schema.closure())
    out = [HEADER.format(doc="liblcf schema of the RPG_RT.ldb structures used by rpgsync."), ""]
    out.append("from typing import Any")
    out.append("")
    out.append("from pydantic import BaseModel, ConfigDict")
    out.append("")
    out.append("")
    out.append("class FieldDef(BaseModel):")
    out.append('    """One chunk of an LCF structure."""')
    out.append("    model_config = ConfigDict(frozen=True)")
    out.append("")
    out.append("    id: int | None           # chunk id (None: part of a packed structure)")
    out.append("    name: str")
    out.append("    type: str                # liblcf type")
    out.append("    kind: str                # LCF codec: int bool str i16vec u8vec i32vec S:<struct> A:<struct> raw")
    out.append("    model: str               # model type: int bool str enum:<E> bits u8 i16 i32 params equip")
    out.append("    #                          flags:<F> struct:<S> array:<S> raw")
    out.append("    default: Any = None      # model-level default (RPG Maker 2000)")
    out.append("    default_2k3: Any = None  # model-level default (RPG Maker 2003)")
    out.append("    persist: bool = False    # written even when equal to the default")
    out.append("    is2k3: bool = False      # RPG Maker 2003 only")
    out.append("    size_id: int | None = None  # chunk holding this field's byte size")
    out.append("    size_of: int | None = None  # this chunk is the byte size of chunk size_of")
    out.append('    doc: str = ""')
    out.append("")
    out.append("")
    out.append("def _f(id, name, type, kind, model, default=None, default_2k3=None, persist=False, is2k3=False,")
    out.append('       size_id=None, size_of=None, doc=""):')
    out.append("    return FieldDef(id=id, name=name, type=type, kind=kind, model=model, default=default,")
    out.append("                    default_2k3=default_2k3, persist=persist, is2k3=is2k3, size_id=size_id,")
    out.append("                    size_of=size_of, doc=doc)")
    out.append("")
    out.append("")
    out.append("# Database table name -> (Database chunk id, structure, title)")
    out.append("TABLES: dict[str, tuple[int, str, str]] = {")
    for name, cid, s, title in TABLES:
        out.append("    %s: (0x%02X, %s, %s)," % (_pyrepr(name), cid, _pyrepr(s), _pyrepr(title)))
    out.append("}")
    out.append("")
    out.append("# Structures used by the exported tables (dependencies first)")
    out.append("MODEL_STRUCTS: list[str] = [")
    for s in schema.closure():
        out.append("    %s," % _pyrepr(s))
    out.append("]")
    out.append("")
    out.append("# Structures whose elements carry an id")
    out.append("INDEXED = {%s}" % ", ".join(_pyrepr(s) for s, has in sorted(schema.structs.items()) if has))
    out.append("")
    out.append("STRUCTS: dict[str, tuple[FieldDef, ...]] = {")
    for s, rows in schema.rows.items():
        if not rows:
            continue
        out.append("    %s: (" % _pyrepr(s))
        sizes = {r.name: r.index for r in rows if r.is_size}
        data = {r.name: r.index for r in rows if not r.is_size}
        for r in rows:
            kind, model = schema.kinds(r, closure)
            if r.is_size:
                kind, model, d2k, d2k3 = "int", "int", None, None
            else:
                d2k, d2k3 = schema.default(r, model)
            args = [
                "0x%02X" % r.index if r.index is not None else "None",
                _pyrepr(r.name),
                _pyrepr(r.type),
                _pyrepr(kind),
                _pyrepr(model),
            ]
            kw = []
            if d2k is not None or d2k3 is not None:
                kw.append("default=%s" % _pyrepr(d2k))
                kw.append("default_2k3=%s" % _pyrepr(d2k3))
            if r.persist:
                kw.append("persist=True")
            if r.is2k3:
                kw.append("is2k3=True")
            if not r.is_size and r.name in sizes:
                kw.append("size_id=0x%02X" % sizes[r.name])
            if r.is_size:
                kw.append("size_of=0x%02X" % data[r.name])
            else:
                kw.append("doc=%s" % _pyrepr(describe(schema, r, model)))
            out.append("        _f(%s)," % ", ".join(args + kw))
        out.append("    ),")
    out.append("}")
    out.append("")
    out.append("ENUMS: dict[str, dict[str, int]] = {")
    for name, values in schema.enums.items():
        out.append("    %s: {%s}," % (_pyrepr(name), ", ".join("%s: %d" % (_pyrepr(k), v) for k, v in values.items())))
    out.append("}")
    out.append("")
    out.append("# Bit flags: name -> [(flag, is2k3)], bit i of the packed bytes is flag i")
    out.append("FLAGS: dict[str, list[tuple[str, bool]]] = {")
    for name, flags in schema.flags.items():
        out.append("    %s: [%s]," % (_pyrepr(name), ", ".join("(%s, %s)" % (_pyrepr(k), v) for k, v in flags)))
    out.append("}")
    return "\n".join(out) + "\n"


def _field_src(name: str, ann: str, dflt: str, doc: str, width: int = 100) -> str:
    """``name: ann = Field(dflt, description=doc)``, wrapped to `width`."""
    line = "    %s: %s = Field(%s, description=%s)" % (name, ann, dflt, _pyrepr(doc))
    if len(line) <= width:
        return line
    if len(_pyrepr(doc)) + 20 <= width:
        return "    %s: %s = Field(\n        %s,\n        description=%s,\n    )" % (name, ann, dflt, _pyrepr(doc))
    pieces, cur = [], ""
    for word in doc.split(" "):
        if cur and len(cur) + len(word) + 1 > width - 16:
            pieces.append(cur + " ")
            cur = word
        else:
            cur = word if not cur else cur + " " + word
    pieces.append(cur)
    body = "\n".join("            %s" % _pyrepr(p) for p in pieces)
    return "    %s: %s = Field(\n        %s,\n        description=(\n%s\n        ),\n    )" % (name, ann, dflt, body)


def gen_db(schema: Schema) -> str:
    closure = schema.closure()
    closure_set = set(closure)
    tables = {s for _, _, s, _ in TABLES}
    used_enums: list[str] = []
    used_flags: list[str] = []
    for s in closure:
        for r in schema.rows[s]:
            if r.is_size:
                continue
            _, model = schema.kinds(r, closure_set)
            if model.startswith("enum:") and model[5:] not in used_enums:
                used_enums.append(model[5:])
            if model.startswith("flags:") and model[6:] not in used_flags:
                used_flags.append(model[6:])

    out = [
        HEADER.format(
            doc="Typed models of the RPG_RT.ldb database tables.\n\n"
            "Database files exported by rpgsync (``Scripts/database/*.py``) hold these models (see rpgsync.tables).\n"
            "Defaults are liblcf's; a keyword left out of a file means the default value."
        ),
        "",
    ]
    out.append("from typing import Any, Literal")
    out.append("")
    out.append("from pydantic import BaseModel, ConfigDict, Field, PrivateAttr")
    out.append("")
    names = sorted(["DbModel"] + [_camel(e) for e in used_enums] + [_camel(f) for f in used_flags] + list(closure))
    out.append("__all__ = [")
    for n in names:
        out.append("    %s," % _pyrepr(n))
    out.append("]")
    out.append("")
    out.append("")
    out.append("def _freeze(v: Any) -> Any:")
    out.append("    if isinstance(v, dict):")
    out.append("        return tuple((k, _freeze(x)) for k, x in v.items())")
    out.append("    if isinstance(v, (list, tuple)):")
    out.append("        return tuple(_freeze(x) for x in v)")
    out.append("    return v")
    out.append("")
    out.append("")
    out.append("class DbModel(BaseModel):")
    out.append('    """Base class of the database models."""')
    out.append('    model_config = ConfigDict(extra="forbid")')
    out.append("    _lineno: int | None = PrivateAttr(default=None)")
    out.append("    _ident: str | None = PrivateAttr(default=None)")
    out.append("")
    out.append("    @property")
    out.append("    def lineno(self) -> int | None:")
    out.append('        """Line of the entry in its database file (None when read from the game)."""')
    out.append("        return self._lineno")
    out.append("")
    out.append("    def key(self) -> tuple:")
    out.append('        """Hashable value used to compare entries."""')
    out.append("        return _freeze(self.model_dump())")
    out.append("")
    out.append("")
    for e in used_enums:
        line = "%s = Literal[%s]" % (_camel(e), ", ".join(_pyrepr(k) for k in schema.enums[e]))
        if len(line) > 100:
            line = "%s = Literal[\n%s\n]" % (_camel(e), "\n".join("    %s," % _pyrepr(k) for k in schema.enums[e]))
        out.append(line)
    out.append("")
    for fl in used_flags:
        out.append("")
        out.append("class %s(DbModel):" % _camel(fl))
        out.append('    """Bit flags %s."""' % fl.replace("_Flags", ""))
        for name, is2k3 in schema.flags[fl]:
            doc = name.replace("_", " ").capitalize() + "." + (" RPG Maker 2003 only." if is2k3 else "")
            out.append("    %s: bool = Field(default=False, description=%s)" % (name, _pyrepr(doc)))
        out.append("")
    for s in closure:
        out.append("")
        out.append("class %s(DbModel):" % s)
        doc = CLASS_DOCS.get(s, s + ".")
        out.append('    """%s"""' % doc)
        out.append("")
        if schema.structs.get(s):
            if s in tables:
                d = "Database id: entry N is the N-th slot of the table (RPG_RT looks entries up by position)."
            else:
                d = "Element id; leave it out to use the position in the list (1-based)."
            out.append(_field_src("id", "int | None", "default=None", d))
        for r in schema.rows[s]:
            if r.is_size:
                continue
            kind, model = schema.kinds(r, closure_set)
            if model == "raw":
                continue
            d2k, d2k3 = schema.default(r, model)
            doc = describe(schema, r, model)
            if d2k != d2k3:
                ann, dflt = "int | None", "default=None"
                doc += " None = the engine default (%s in RPG Maker 2000, %s in RPG Maker 2003)." % (d2k, d2k3)
            elif model == "int":
                ann, dflt = "int", "default=%s" % _pyrepr(d2k)
            elif model == "bool":
                ann, dflt = "bool", "default=%s" % _pyrepr(d2k)
            elif model == "str":
                ann, dflt = "str", "default=%s" % _pyrepr(d2k)
            elif model.startswith("enum:"):
                ann, dflt = "%s | int" % _camel(model[5:]), "default=%s" % _pyrepr(d2k)
            elif model == "bits":
                ann, dflt = "list[bool]", "default_factory=list"
            elif model in ("u8", "i16", "i32"):
                ann, dflt = "list[int]", "default_factory=list"
            elif model == "params":
                ann, dflt = "Parameters", "default_factory=Parameters"
            elif model == "equip":
                ann, dflt = "Equipment", "default_factory=Equipment"
            elif model.startswith("flags:"):
                ann, dflt = _camel(model[6:]), "default_factory=%s" % _camel(model[6:])
            elif model.startswith("struct:"):
                ann, dflt = model[7:], "default_factory=%s" % model[7:]
            elif model.startswith("array:"):
                ann, dflt = "list[%s]" % model[6:], "default_factory=list"
            else:
                raise SystemExit("no model type for %s" % model)
            out.append(_field_src(r.name, ann, dflt, doc))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


app = typer.Typer(add_completion=False)


@app.command()
def main(
    csv_dir: str = typer.Option(DEFAULT_CSV, help="liblcf generator/csv folder"),
    out_dir: str = typer.Option(DEFAULT_OUT, help="where to write dbschema.py and db.py"),
):
    """Regenerate rpgsync/dbschema.py and rpgsync/db.py."""
    schema = Schema(csv_dir)
    for name, text in (("dbschema.py", gen_dbschema(schema)), ("db.py", gen_db(schema))):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        typer.echo("wrote %s" % os.path.normpath(path))


if __name__ == "__main__":
    app()
