# Changelog

## Versions

rpgsync is released on [PyPI](https://pypi.org/project/rpgsync/) as `0.MINOR.PATCH`:

- **MINOR** goes up when the scripts change: `rpgsync pull` writes a game's scripts
  differently (another layout, other names, another way to write a command), or scripts
  written by it may not work with the previous minor version (a DSL name renamed or
  removed).
- **PATCH** goes up for everything else: fixes and additions after which `rpgsync pull`
  writes the same scripts.

A game's `Scripts/pyproject.toml` depends on the version that set it up,
`rpgsync~=0.MINOR.PATCH` (that version and its later patches), and its `Scripts/uv.lock`
pins the exact one. [Upgrading rpgsync](README.md#upgrading-rpgsync) moves a game to a
newer one; [Releasing](docs/development.md#releasing) makes a new one.

## 0.2.0

The scripts of Maniac Patch games change: `rpgsync pull` writes them differently.

- Maniac Patch commands have typed, named forms: `maniac.show_string_picture(...)`,
  `maniac.console_print(...)` & co, `maniac.get_media_option(...)` /
  `set_media_option(...)`, `maniac.get_game_info("map_size", into=...)`, and
  `maniac.script("line 1", "line 2", results=[...])` holding a script and its further
  lines in one call. `write_picture` with all its parameters zero is no longer raw.
- String variables: `database/strings.py` names them, and Control Strings are Python
  statements on `strings` (`strings.name = "Alex"`, `+=`, `str(variables.n).rjust(4, "0")`,
  `db_name("item", 3)`, `read_file(...)`, `.replace()`, `int()`, `len()`, `.find()`,
  `.split(into=...)`, `.pop_line()`, `.to_file()`), and Conditional Branch compares them
  (`if strings.name == "Alex":`, `"x" in strings[1]`).
- Maniac options of standard commands are Python: Control Variables operands and
  operations (`variables.x = variables.a * 2`, `sqrt()`, `sin()`, `min()`, `abs()`,
  `lerp()`, `array_sum()`, `party[0].hp`, `game.frames`, `switches[n]`, `a if b > c else d`,
  `variables[10:1:-1]` backwards ranges, `variables[variables.a:variables.b + 1]`), loops
  (`for variables.i in range(1, 11):`, `while variables.hp > 0:`, `continue`,
  `break_loop(levels=2)`), `wait(frames=...)`, `common_events[variables.n]()`,
  `events[n].erase()` / `.restore()`, names and values from variables in `play_bgm()`,
  `play_se()`, `face()`, `message_options(width=..., font=...)`, pixel scrolling
  (`pan_screen_by()`, `pan_screen_to()`), `call_system_function()`,
  `disable_system_functions()`.
- The Maniac Patch's editor stores some standard commands with other parameters (a
  Control Variables of one variable with 0 as last id, zero parameters more on loops,
  faces without their trailing zeros...): rpgsync detects the layout most of a game
  uses, writes the same Python for both, and marks a command stored the other way
  with `# layout: maniac` / `# layout: rpg_maker`. A 2003 game's Wait without its key
  parameter is `wait(1.0, key=None)` instead of a raw line.
- `database/terms.py` and `database/system.py` export the Terms and the System settings
  (generated from liblcf's schema: a field named there appears once rpgsync's schema
  is generated again; chunks it doesn't know are kept). The Maniac Patch's System
  fields are named: title and save scene hooks, message window font, size, spacing and
  face layout. Skill `failure_message` 3 (Maniac's Hit Rate Calc) and `-9` in
  `menu_commands` (Game End hidden) are documented.
- Common events run on `"battle_start"` and `"battle_parallel"` (Maniac Patch triggers
  6 and 7).
- `rpgsync check`: in a game declaring `"maniac"`, `database/game_types.py` lets
  `variables[n]` stand where the patch reads a value (`randint(1, variables.max)`);
  without it, Maniac options of standard commands are reported as warnings.
- `tools/gen_dbschema.py` reads the EasyRPG Editor's liblcf (`../Editor/lib/liblcf`) by
  default.

## 0.1.2

- `rpgsync check --resources` names every file in its warnings, instead of the first 8 and
  how many more: the files the database's entries use, sorted, with their number, and
  the imported files never used. A list too long for its line goes under it, wrapped to
  the terminal's width without cutting a name. A file used several times in one game file
  (`Map0003.lmu`) is no longer named again in "also used at".
- `rpgsync check --resources` reads a name DynParams builds for a command
  (`pictures[2].show(f"w{variables.sheet}")`) whole, in that command's folder, numbers
  from variables as digits: `Picture/w4.png` counts as used, as a name of one letter
  then a number is no longer too short to tell, and `Picture/wall.png` doesn't. The name
  of a command whose parameters alone DynParams rewrites is checked like any other.

## 0.1.1

- `rpgsync check`: the pages that one later page keeps from ever being active are
  named in one warning, in fewer words.
- `rpgsync check` names files relative to the current folder (`Scripts/Map0004.py:12`
  run from the game folder), ty's problems too, so terminals and editors can open them.
- `rpgsync check --resources`: a name ending with a space (`"Theme "`, whose file is
  `Theme .mp3`) is found, instead of reported as not imported and its file as never used.
- rpgsync knows CortiXCustomCrit.dll (Corti's critical hits): its `dyn.ccrit_*`
  commands, and its `[CortiCustomCrit]` and `[CortiLog]` sections of `DynRPG.ini`.

## 0.1.0

First release on PyPI.

- A new game's scripts depend on `rpgsync~=0.1.0` from PyPI, pinned by their `uv.lock`,
  instead of a link to this computer's rpgsync folder.
- A developer of rpgsync links their rpgsync folder as `Scripts/.rpgsync/rpgsync`:
  rpgsync installs it, editable, in the scripts' environment, and leaves
  `pyproject.toml` and `uv.lock` as they are.
