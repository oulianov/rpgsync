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
- **`allow_commands`** allows single command codes, e.g. one EasyRPG command,
  without allowing the whole patch.
- **`engine = "2000"`** on a 2003 game keeps it compatible with RPG Maker 2000.

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

- **In scripts:** `@name args` becomes `dyn.name(args)`; variable tokens such as
  `V152` are written `V[152]`:

  ```python
  dyn.dynparams_add_param(2, V[152])  # @dynparams_add_param 2, V152
  dyn.change_text("desc-indice", r"\I[\v[153]]", 0)
  ```

- **Any plugin works**: rpgsync doesn't know plugins, so every call becomes
  `dyn.name(...)`, whether a plugin exists for it or not.
- **Arguments that don't fit** the `@name arg, arg` format keep the comment as
  is: `comment("@call easyrpg_add, 1, 10")`.
- **Plugins that hook the engine** without event calls (battle plugins,
  graphics plugins configured by `.ini` files) don't appear in scripts.
- **`check`:** needs `"dynrpg"` in `patches`.
- **EasyRPG Player** emulates a few plugins (DynText, DynParams, its own
  `easyrpg_*` functions). Other calls log `Unsupported DynRPG function` and do
  nothing; engine plugins are ignored.

## Maniac Patch

New event commands (codes 3001-3038), and extensions of standard commands.

- **In scripts:** every Maniac command is `maniac.name(...)`. Commands with a
  known layout take named arguments; values take any of Maniac's forms: a
  number, `variables[n]`, `variables[variables[n]]` or `switches[n]`:

  ```python
  maniac.get_mouse_position(x=variables.mouse_x, y=variables.mouse_y)
  maniac.save(slot=variables.slot, result=variables.saved)
  maniac.rewrite_map(tile=91, x=variables[3], y=variables[4], width=1, height=1, layer="upper")
  ```

- **Named arguments:** saves and loads, mouse, key input, picture info and
  pixels, picture ids, map tiles, global save, game options, variable arrays,
  message hooks, zoom, battle commands (22 commands).
- **Raw parameters** (positional numbers) for the others:
  `control_strings`, `show_string_picture`, `get_game_info`,
  `control_self_variable`, `script`, `console`, `call_command`, ...:

  ```python
  maniac.control_strings(0, 1, 0, 0, 7, 0, 0, text="no zoom")
  ```

  A named command also falls back to raw parameters when its values don't fit
  its named form (a selector rpgsync doesn't know).
- **Unknown codes** are `maniac.command_NNNN(...)`.
- **Extensions of standard commands** (Control Variables with Maniac operands,
  pictures with Maniac options, loops with conditions, Call Event by variable)
  are raw `cmd("ControlVars", ...)` lines.
- **Self variables** (negative ids) are written `variables[-1]`.
- **`check`:** needs `"maniac"` in `patches`.

## EasyRPG commands

EasyRPG Player's own event commands (codes 2000-2999: JSON processing, clone
or destroy map events, pathfinding, ...).

- **In scripts:** raw lines, `cmd(2055, 0, 0, 1, ..., text="/array")`.
- **`check`:** needs `"easyrpg"` in `patches`, or the codes in `allow_commands`.
- These commands only run in EasyRPG Player.

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
