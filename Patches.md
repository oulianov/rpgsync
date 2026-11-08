# Patches

How rpgsync handles each RPG Maker 2000/2003 patch: what it detects, how the
patch shows in your scripts, and what `rpgsync check` reports.

Whatever the patch, **a sync never loses data**: anything rpgsync can't write
as readable Python is kept as a raw line, and syncs back byte for byte.

## Settings

The first sync writes the detected patches into `Scripts/pyproject.toml`.
Edit them there:

```toml
[tool.rpgsync]
engine = "2003"               # "2000" | "2003": the engine the checks target
patches = ["dynrpg"]          # "dynrpg" | "maniac" | "easyrpg"
allow_commands = [2055]       # single extra command codes, allowed anyway
```

- **`patches`** allows a patch's commands: without it, `rpgsync check`, the
  generated tests and the sync refuse them (the game file is not written).
  DynRPG calls are the exception: without the patch they are plain comments,
  so `rpgsync check` only warns.
- **`allow_commands`** allows single command codes, e.g. one EasyRPG command,
  without allowing the whole patch.
- **`engine = "2000"`** on a 2003 game keeps it compatible with RPG Maker 2000:
  a command that only exists in 2003 is an error, a 2003 parameter of a
  command 2000 has is a warning (RPG Maker 2000 ignores it).

rpgsync detects:

| Found in the game | Patch |
|---|---|
| a `DynPlugins/` folder | `dynrpg` |
| `DynRPG=1` in the `[Patch]` section of `EasyRPG.ini` | `dynrpg` |
| `Maniac=1` in `EasyRPG.ini` | `maniac` |
| `EasyRPG=1` in `EasyRPG.ini` | `easyrpg` |

EasyRPG Player detects patches by itself from their files (`dynloader.dll`
DynRPG, `accord.dll` Maniac, `warp.dll` Power Mode 2003, `harmony.dll` Ineluki
Key Patch), and reads the same `[Patch]` section of `EasyRPG.ini` otherwise:
`DynRPG=1`, `Maniac=1`, `EasyRPG=1`, `PowerMode2003=1`, `KeyPatch=1`,
`PicUnlock=1`, `CommonThisEvent=1`, ...

## DynRPG

Plugins (`DynPlugins/*.dll`) called from events through comments that start
with `@`.

- **In scripts:** `@name args` becomes `dyn.name(args)`. Tokens are written
  like the rest of the scripts: `V152` is `variables.name` (or `variables[152]`),
  `VV3` is `variables[variables.name]`, `N3` is `actors[3].name` and `NV3` is
  `actors[variables.name].name`. Other spellings keep their form (`v[150]`).
  A `"` inside a string is written `""` in the comment. Unquoted text is a
  **word**: `word.end` when it is a Python name, `word("...")` otherwise, both
  written as is (spaces kept; no `"`, `,` or line break):

  ```python
  dyn.dynparams_add_param(2, variables.clue_switch)  # @dynparams_add_param 2, V152
  dyn.change_text("desc-indice", r"\I[\v[153]]", 0)
  dyn.write_text("id", 10, 20, 'He said "hi"')      # @write_text "id", 10, 20, "He said ""hi"""
  dyn.remove_text("menu1", word.end)                 # @remove_text "menu1", end
  dyn.dynparams_append_string(word(" Page 2"))       # @dynparams_append_string  Page 2
  dyn.call(word.easyrpg_add, 1, 10)                  # @call easyrpg_add, 1, 10
  ```

  A comment becomes a call only when the call compiles back to the same text:
  the first argument after one space, the others after `, `. Anything else
  keeps the comment as is: `comment('@write_text "id", 1, 2,  "two spaces"')`.

- **DynParams as plain Python:** with `DynPlugins/DynParams.dll`, a variable
  where RPG Maker takes a number compiles to the DynParams comments, and the
  decompiler folds them back (see the README):

  ```python
  if switches[variables.clue_switch]:  # placeholder: switches.relm
  ```

  - **Compiling:** a statement that does not compile is tried again with the
    fewest of its variables (and f-strings of variables) put through DynParams.
    The parameter a variable lands in is found by compiling the command with
    1, then 2, in its place. It must be stored as the number itself.
  - **Placeholder:** the command holds 1 (`""` for a text), or the values of a
    `# placeholder: a, b` comment on the statement, one per dynamic value in
    source order. A value replaces the whole `pictures[...]`, or the variable alone.
  - **Folding:** a run of `@dynparams_add_param N, V..` (or `VV..`),
    `@dynparams_append_string` and `@dynparams_append_number` comments, then
    `@dynparams_overwrite_next` right before its command, is written in this
    form when it compiles back to the same commands. Everything else (a stat
    chosen by a variable, parameters built across other commands, other spellings)
    stays as `dyn.*` lines with hints.
  - **Type check:** where a command keeps a fixed number, the DSL takes a
    `Fixed`: `int`, plus `Variable` in a game that runs DynParams. rpgsync
    writes which one into `Scripts/database/game_types.py`, which ty and Pylance
    read, so they flag a variable there only in games without the plugin.

- **DynParams hints:** each `dyn.dynparams_add_param(...)` gets a hint naming
  the parameter it overwrites, and `dyn.dynparams_overwrite_next()` the command
  it rewrites:

  ```python
  dyn.dynparams_add_param(2, variables.clue_switch)  # → ConditionalBranch: switch
  dyn.dynparams_overwrite_next()  # → ConditionalBranch
  if switches.relm:
  ```

  A hint is only written when the rewritten command is in the same block.

- **DynParams checks** (`rpgsync check`). An **error** breaks the game in
  RPG_RT; like the other compatibility problems, it fails `rpgsync check` and
  the generated tests, and stops the sync from writing the event into the
  game. A **warning** does not break the game: `rpgsync check` shows it and
  still succeeds.

  | Problem | Level | In RPG_RT |
  |---|---|---|
  | A parameter the target command does not have (`add_param 9` on a Wait) | error | DynParams writes outside the command: crash or corrupted memory |
  | A parameter index below 1 | error | same |
  | A message line outside 1-4, a choice below 1 | error | DynParams shows an error box |
  | An unknown `@dynparams_...` command (a typo) | warning | ignored |
  | `dyn.dynparams_overwrite_next()` with no command after it in its block | warning | rewrites whatever runs next (an else, the next command after the block) |
  | Parameters with no `dynparams_overwrite_next()` after them | warning | never applied |
  | More message lines or choices than the target has | warning | the extra ones are not shown |
  | Message lines or choices for another kind of command | warning | they do not change it |

- **Known plugins:** rpgsync knows the commands of some plugins, and the
  `DynRPG.ini` settings they read (`rpgsync/dynrpg.py`; any other plugin's
  call is written `dyn.name(...)` too):

  | Plugin | Commands | DynRPG.ini |
  |---|---|---|
  | DynTextPlugin.dll | `write_text(id, x, y, text, fixed?, color?, picture?)` (`fixed`: `word.fixed`, written `"fixed"`), `append_line(id, text, end?)`, `append_text(id, text, end?)`, `change_text(id, text, color)` (`word.end`: same color), `change_position(id, x, y)`, `remove_text(id, end?)`, `remove_all()`; `end`: anything (`word.end`) | `[DynTextPlugin]` PictureID |
  | DynParams.dll | the `dynparams_*` commands above | `[DynParams]` DebugLog |
  | msg_commands.dll | `process_cmd()` | |
  | EasyRPG Player (no DLL) | `call(function, ...)`, `easyrpg_output(mode, message, ...)`, `easyrpg_add(variable, value, ...)`, any `easyrpg_*` | |
  | faster_atb_advanced.dll | none: it hooks the engine | `[faster_atb_advanced]` BattleSpeedVariable, DefaultSpeed |
  | game_clock.dll | none | `[game_clock]` SessionStart ... BackgroundVar (variables), ForegroundImage(N), BackgroundImage(N), DummyImage, TimeTextDisplay |
  | ExpBug-in.dll | none | `[Exp_Curves]` CoefficientA-E |
  | ATBWaitOnly.dll | none | |
  | CortiXCustomCrit.dll | `ccrit_getstatus(hero, switch)`, `ccrit_resetstatus(hero)`, `ccrit_monster_getstatus(monster, switch)`, `ccrit_monster_resetstatus(monster, unused)`, `ccrit_skill_addcon/addvar/mulcon/mulvar/delay(skill, value)` | `[CortiCustomCrit]` CritCalcType, CritAttackBonusType(Value), CritCalcWeaponCriticalType, CritCalcWeaponCritBonusApplication (needed), t1/t2/t3..., Monster..., Skill...(N), VisualEffect..., DelayFrames...; `[CortiLog]` LogToConsole, LogToFile |

  Their commands are typed methods of `dyn` (editor completion, ty): numbers
  take `12`, `variables.x`, `variables[...]` or `v[12]`; texts take `"..."` or
  `actors[...].name`; words take `word...`, written unquoted, except a plugin's
  own words, written the way it reads them (`word.fixed` of `write_text`: the
  `"fixed"` DynText compares). Arguments are positional, and more
  than a command reads are accepted (a plugin version may read them).
- **Plugins that hook the engine** without event calls (battle plugins,
  graphics plugins configured by `.ini` files) don't appear in scripts.
- **`check`:** without `"dynrpg"` in `patches`, each call is a warning: RPG_RT
  runs it as a plain comment. With `"easyrpg"`, `dyn.easyrpg_...()` calls are
  fine: EasyRPG Player runs `@easyrpg_` comments without DynRPG. With
  `"dynrpg"`, `rpgsync check` also warns (the sync still writes the event):

  | Problem | Example |
  |---|---|
  | Fewer arguments than a known command needs | `dyn.change_text() needs 3 arguments (id, text, color), got 2` |
  | An argument of the wrong kind | `dyn.change_position(): x (argument 2) is a number, not "10"` |
  | More arguments than the command reads | `dyn.change_position() reads 3 arguments (id, x, y): 2 more are ignored` |
  | A plugin's own word written another way: bare (`@write_text "a", 1, 2, "x", fixed` stays a `comment(...)`, `word.fixed` writes `"fixed"`) or in another case | `dyn.write_text(): fixed (argument 5) works as the "string" "fixed" only (word.fixed), not the word fixed: the plugin ignores it (EasyRPG Player reads it)` |
  | A literal id the database lacks, in a DynText string (`\v[n]` variable, `\n[n]` actor, `\i[n]` / `\I[n]` item, `\t[n]` / `\T[n]` skill; `\i[\v[7]]` is dynamic) or a token (`V12`, `N3`) | `dyn.write_text(): \v[4000]: the database has no variable 4000 (it has 3327)` |
  | A command of a known plugin that isn't in `DynPlugins/` (any case) | `dyn.process_cmd() comes from msg_commands.dll, which isn't in DynPlugins` |
  | An unknown command, when every DLL of `DynPlugins/` is known (an unknown DLL may provide it) | `dyn.foo(): no plugin in DynPlugins provides it` |
  | An EasyRPG Player command without `"easyrpg"` in `patches` | `dyn.easyrpg_add() is an EasyRPG Player command, RPG_RT ignores it` |
  | A `DynRPG.ini` section no installed plugin reads (`[QuickPatches]` is DynRPG's; a plugin reads the section named after its DLL) | `DynRPG.ini: [CortiLog]: no plugin in DynPlugins reads this section` |
  | Settings an installed plugin needs, missing | `DynRPG.ini: [game_clock] lacks SessionStart: game_clock.dll needs it` |
  | A plugin setting naming a variable past the database | `DynRPG.ini: [game_clock] names variables past the database's 3327 (...)` |

  A plugin EasyRPG Player emulates (DynText, DynParams) needs no DLL with
  `"easyrpg"` in `patches`. The `DynRPG.ini` lines show when the whole game
  is checked.
- **EasyRPG Player** emulates a few plugins (DynText, DynParams, its own
  `easyrpg_*` functions). Other calls log `Unsupported DynRPG function` and do
  nothing; engine plugins are ignored.

## Maniac Patch

New event commands (codes 3001-3038), extensions of standard commands, string
variables, and new database fields.

- **Values** take any of Maniac's forms where the patch reads one: a number,
  `variables[n]`, `variables[variables[n]]` or `switches[n]`. Variables hold any
  32-bit number (`variables[1] = 2147483647`).
- **Maniac commands** are `maniac.name(...)`, with named arguments:

  ```python
  maniac.get_mouse_position(x=variables.mouse_x, y=variables.mouse_y)
  maniac.save(slot=variables.slot, result=variables.saved)
  maniac.show_string_picture(picture=1, x=160, y=120, text="Hello \\v[1]", system="System2")
  maniac.console_print(strings.log, expand=True)
  maniac.set_media_option("show_fps", 1)
  maniac.get_game_info("map_size", into=variables.width)
  maniac.script("v[1] = 123;", "t[2] = 'a';", results=[variables[1], strings[2]])
  ```

  Saves, mouse, keys, pictures (string pictures, info, pixels, ids, output), map
  tiles, global save, game options, variable arrays, message hooks, zoom, battle,
  console, media options, scripts (one call holding every line), game info.
- **String variables** (Control Strings) are Python statements on `strings`,
  named in `database/strings.py` like the variables:

  ```python
  strings.name = "Alex"
  strings[2] = str(variables.gold).rjust(6, "0")
  strings[3] = db_name("item", variables.item)
  variables.n = len(strings.name)
  variables.at = strings[1].find("sit")
  if strings.answer == "yes":
      ...
  ```

- **Standard commands** take Maniac's options as Python:

  ```python
  variables.x = variables.a * 2              # Control Variables: one operation, functions
  variables.d = sqrt(variables.n, 10)         # sqrt, sin, cos, atan2, min, max, abs, lerp...
  variables.hp = party[0].hp                  # party members, game.date, game.frames...
  for variables.i in range(1, 11):            # Loop: count, count up / down, while
      continue                                # Break Loop: next turn
  while variables.hp > 0:
      wait(frames=1)                          # Wait in frames, or by variable
  common_events[variables.ce]()               # Call Event by variable
  events[variables.target].erase()            # Erase Event of another event, restore()
  play_bgm(strings.music, volume=variables.v) # names from string variables
  face(strings.face, variables.index)
  message_options(width=320, height=96, font="Arial")
  pan_screen_to(160, 120, frames=60, wait=True)
  call_system_function("load")                # Call System Functions
  disable_system_functions(save=True)         # Control System Functions
  ```

- **Layouts:** the Maniac Patch's editor stores some standard commands with other
  parameters for the same meaning (a Control Variables of one variable has 0 as
  its last id, a loop or a Break Loop has zero parameters more). rpgsync detects
  the layout most of a game uses (kept in `.rpgsync/config.json`) and writes the
  same Python either way; a command stored the other way says so at the end of
  its line: `variables[1] = 5  # layout: rpg_maker`.
- **Raw parameters** (positional numbers) where the layout is unknown or a value
  doesn't fit the named form: `maniac.control_self_variable(...)` (its layout is
  only guessed), `call_command`, `add_move_route`, a string picture whose text
  comes from a string variable, more than 4 choices, Conditional Branch with an
  expression or ignoring case...: `maniac.control_strings(0, 1, 0, 0, 7, 0, 0, text="no zoom")`.
- **Unknown codes** are `maniac.command_NNNN(...)`.
- **Database:** `database/strings.py` (string variables), `database/terms.py` and
  `database/system.py` hold Maniac's fields (title and save scene hooks, message
  window font and layout...); common events run on `"battle_start"` and
  `"battle_parallel"`.
- **Self variables** (negative ids) are written `variables[-1]`.
- **`check`:** needs `"maniac"` in `patches`; without it, Maniac options of
  standard commands are reported as warnings. With it, `database/game_types.py`
  lets variables stand where the patch reads them (`randint(1, variables.max)`).

## EasyRPG commands

EasyRPG Player's own event commands (codes 2000-2999: pathfinding, JSON, clone or
destroy map events, ...). EasyRPG Player runs them with `EasyRPG=1` in the `[Patch]`
section of `EasyRPG.ini`; RPG_RT doesn't know them.

- **EasyRPG commands** are `easyrpg.name(...)`, with named arguments, typed in
  `rpgsync/dsl.py` (class `EasyRPG`):

  ```python
  easyrpg.trigger_event_at(x=variables.x, y=variables.y, face_player=False)
  easyrpg.pathfinder(event=player, x=19, y=20, diagonal=True, ignore=[haru], frequency=8)
  easyrpg.wait_for_single_movement(event=player, max_failures=10, result=variables.arrived)
  easyrpg.set_interpreter_flag(patch="maniac", on=True)
  easyrpg.process_json(op="get", json=strings.save, path="/gold", target=variables.gold)
  easyrpg.clone_map_event(map=maps.lab, event=3, x=7, y=2, name="Copy")
  easyrpg.destroy_map_event(event=events[variables.target])
  call_system_function("language")  # Maniac's Call System Functions: EasyRPG Player's scenes
  ```

  | Command | Arguments |
  |---|---|
  | `trigger_event_at` (2002) | `x`, `y`; `face_player` (left out: the command's first version, which turns the event to the hero) |
  | `pathfinder` (2003) | `event`, `x`, `y`, `search_limit` (500), `max_steps` (-1: all), `while_moving` (`"replace"`, `"wait"`, `"skip"`), `diagonal`, `skippable`, `debug`, `ignore` (a list of events, or `variables[variables[id]]`: how many, then their ids), `frequency` (left out: not stored, EasyRPG Player uses 3) |
  | `wait_for_single_movement` (2051) | `event`, `max_failures` (0: until the route ends), `result` (a variable: 1 when the route ends, 0 when given up) |
  | `set_interpreter_flag` (2053) | `patch` (`"destiny"`, `"dynrpg"`, `"maniac"`, `"common-this"`, `"pic-unlock"`, `"key-patch"`, `"rpg2k3-cmds"` or `"rpg2k3-commands"`, `"rpg2k-battle"`, or its number 1-8), `on` |
  | `process_json` (2055) | `op` (`"get"`, `"set"`, `"get_length"`, `"get_keys"`, `"get_type"`, `"remove"`, `"push"`, `"pop"`, `"contains"`), `json`, `path` (a JSON pointer), `target` (a switch, variable or string variable), `expand` / `hex` (the path's `\V[n]` codes replaced), `pretty`, `keys_object` |
  | `clone_map_event` (2056) | `map` (0: this one), `event` (its id), `x`, `y`, `new_id` (0: the next free one), `name` ("": the source's) |
  | `destroy_map_event` (2057) | `event` (left out: the event running it) |

- **Values** are `EasyValue`: a number, `variables[n]` or `variables[variables[n]]`
  (EasyRPG Player reads the pointer with the Maniac Patch only). An event is `this`,
  `player`, a vehicle, `events[id]`, an event of the map by name, or
  `events[variables[n]]`. A switch, variable or string variable the command reads or
  writes is named by id, `variables[n]` or `variables[variables[n]]`:
  `target=strings[variables.slot]`. Texts (the JSON, its path, a name) are a text
  the command holds, `strings[n]` or `strings[variables[n]]`. When Process JSON holds
  both, its text is the path, then the JSON (EasyRPG Player reads the path first; the
  EasyRPG wiki says the reverse); their lengths count characters, not bytes.
- **Raw parameters** (positional numbers) where a value doesn't fit the named form:
  a patch name in another case (`"Maniac"`; EasyRPG Player reads any case), a value
  read from a switch, Process JSON's target kind from a variable, Pathfinder waiting
  and skipping at once, a command shorter than EasyRPG Player reads. Also for the
  commands liblcf knows that EasyRPG Player doesn't run: `easyrpg.call_movement_action(...)`
  (2050), `easyrpg.animate_variable(...)` (2052), `easyrpg.string_picture_menu(...)`
  (2058). Other codes are `easyrpg.command_NNNN(...)`, and `cmd(2055, ...)` lines of
  older scripts still sync.
- **Call System Functions** (the Maniac Patch's Open Save Menu) opens the scenes of
  EasyRPG Player's menu: `call_system_function("main_menu")`, `"items"`, `"order"`,
  `"settings"`, `"language"` (its language menu, for a game with translations).
  Skills, equipment and status take a hero: raw parameters.
- **`check`:** an EasyRPG command needs `"easyrpg"` in `patches` (or its code in
  `allow_commands`). Warnings: an EasyRPG command using what EasyRPG Player reads with
  the Maniac Patch only (`variables[variables[n]]`, a string variable as Process JSON's
  target, a clone's name from a string variable) without `"maniac"`; one of EasyRPG
  Player's scenes in Call System Functions without `"easyrpg"`.

## Power Mode 2003

An RPG_RT patch (`warp.dll`) that turns variables 1-8 into control registers:
load menu and quit (V1), mouse position (V2-V3), any key (V4), float maths
(V5-V7), picture rotations (V8).

- **In scripts:** nothing special: the registers are ordinary variables. Name
  them in `database/variables.py` (`cro`, `mouse_x`, `key`, ...).
- **`check`:** nothing to declare (no new commands).
- **EasyRPG Player** supports it: detected from `warp.dll`, or
  `PowerMode2003=1` in `EasyRPG.ini`.

## Ineluki Key Patch

Plays "sounds" whose names end in `.script` or `.link` to read keys, the mouse
or play MP3s.

- **In scripts:** ordinary sound calls, `play_se("key_on.script")`.
- **`check`:** nothing to declare.
- **EasyRPG Player** supports it: detected from `harmony.dll`, or
  `KeyPatch=1` in `EasyRPG.ini`.

## Engine-only patches

Patches that change how RPG_RT behaves without new event commands: Unlock
Pictures, Common This Event, Anti-Lag Switch, Direct Menu, MonSca, EXPlus,
GuardRevamp, the RPG Maker 2003 commands patch for 2000, ...

- **In scripts:** nothing to see: they read and write ordinary switches and
  variables, or change engine rules.
- **`check`:** nothing to declare.
- **EasyRPG Player:** enable them in the `[Patch]` section of `EasyRPG.ini`.

## Protected games

Some games scramble the header of their data files (`LcfMapUnit` replaced by
`NNVLLFIHPU`) so that the editor refuses to open them; the engine doesn't
read it.

- **rpgsync** reads them, and writes the scrambled header back unchanged.
- The editor still refuses the files: open them with rpgsync only.

## Unsupported patches

A patch rpgsync doesn't know is still safe to sync:

- **Its commands** are raw `cmd(code, ..., text="...")` lines, which sync back
  exactly. `check` reports `unknown command N` until you add the code to
  `allow_commands`.
- **Its comments** (scripts written in event comments) stay comments.
- **Its engine changes** don't appear in scripts.

See [Tested_Games.md](Tested_Games.md) for the games each patch was tested on.
