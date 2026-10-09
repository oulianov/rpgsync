# rpgsync

A python twin of your RPG Maker 2000/2003 game.

**rpgsync** turns the events of your game into readable Python scripts, and keeps both sides in sync while you work.

- Edit events in your favourite editor, with autocompletion, type checking, search and git. 
- Translate dialogs and database more easily
- Implement automated tests to verify game mechanics 
- Improve your game using the modern developer's toolkit


## Code example

```python
@event(10, "Jeremy", x=15, y=38)
def linky():
    @page(when=variables[102] == 1, sprite=("heroes1", 1), layer="same")
    def page_1():
        # Greetings (this comment is stored in the game)
        text(r"\n[3] : Bonjour, je suis \c[2]Jeremy\c[0].")
        this.move(move_up * 3, face_player, frequency=8)
        ulysse.move(move_up * 2)  # another event of this map, by name
        if party.gold >= 100:
            match show_choices("Payer", "Partir"):
                case "Payer":
                    party.gold -= 100
                    items[3].count += 1
                case "Partir":
                    return
        else:
            text("Reviens avec de l'or.")
        variables[102] += 1


@event(11, "Ulysse", x=10, y=42)
def ulysse(): # The other event!
    @page(sprite=("heroes1", 0), layer="same")
    def page_1():
        pass
```

Every command, option and value is typed and documented in
[`rpgsync/dsl.py`](rpgsync/dsl.py): your editor shows the docs on hover and
flags wrong values (a misspelled direction, a sprite index above 7, ...).

## Demo 

A Doom-like 3d game in RPGMaker using only pictures of vertical slices. The tedious, repetitive event code has been automatically generated. 

https://github.com/user-attachments/assets/9246ff34-b226-4ebc-b8a3-e55b3adcc5d5



## Compatibility

rpgsync works with **RPG Maker 2000 and 2003** games, both in old versions (1.08) and Steam version (1.12a). It was tested on EasyRPG's test suites and on complete
games. [Learn more.](Tested_Games.md).

**Supported Patches:** Everything! DynRPG, Maniac Patch, EasyRPG commands, Power Mode 2003, Ineluki Key Patch... Patches with unknown commands are kept as raw. Patches that only change the engine don't matter for this module. 


## Getting started

### 1. Install uv

rpgsync uses [uv](https://docs.astral.sh/uv/) to manage Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)

### 2. Get rpgsync

```bash
git clone https://github.com/oulianov/rpgsync.git
cd rpgsync
uv tool install --editable .
```

This puts the `rpgsync` command on your PATH.

### 3. Start auto-sync

Run rpgsync in your game folder (the one containing `RPG_RT.ldb`):

```bash
cd /path/to/MyGame
rpgsync
```

-  The first time, it writes the scripts into `/path/to/MyGame/Scripts` and sets that folder up as a small Python project. 
- Open the `Scripts` folder in your editor and select its `.venv` as the Python interpreter.
- Leave `rpgsync` running while you work. Every save on either side is synced within a second.

Before writing a script into the game, rpgsync runs the same checks as the
[automated tests](#automated-tests).:
- The script must compile, and its events must only use commands your engine supports. 
- Problems are reported with their line number and the game file is left untouched until you fix them.
- **The game files are only written once the scripts are valid.**

### Will this break my project?

rpgsync is built so that **it should not**. If anything, you can always go back.

- **Invalid scripts are never written.** A typo, a wrong value or a
  RPG Maker 2003 command in a 2000 game stops at the checks above.
- **Every overwritten file is backed up** (game files and scripts) in
  `Scripts/.rpgsync/history/`, the last 50 syncs. `rpgsync clean`
  deletes them once you no longer need them.
- Only what you changed is written.
- If the same event changed on both sides,
  the editor's version is kept and your script is saved next to it as
  `MapXXXX.conflict-<time>.py`.
- **The RPG Maker editor does not reload files by itself.** Save in the
  editor before editing the same map's script, and reopen the project in
  the editor after script changes; otherwise its next save overwrites them.

Using git on the game folder and doing backups is still a good idea.

## Advanced workflows

### Push and pull changes manually

Instead of the live auto-sync:

```bash
rpgsync status   # what is pending, on which side (git-style)
rpgsync check    # compile and check every script, write nothing
rpgsync pull     # game -> scripts (regenerate the scripts)
rpgsync push     # scripts -> game
```

- Each command works on the game of the current folder, or on the game or `Scripts` folder given as argument. 
- `--map N` limits a command to one map
- `pull --force` overwrites scripts that have unsynced edits.

### Automated tests

The `Scripts` folder comes with tests in `Scripts/tests/`. Keep them: the
first two are also run by rpgsync itself, and **changes only go from a
script into the game if they pass**. A failing script is reported and the
game file stays as it was.

```bash
cd /path/to/MyGame/Scripts
rpgsync check
```

### Database and Common Events

The database (`RPG_RT.ldb`): heroes, classes, skills, items, enemies,
troops, states, variables, switches, common events... is written to
`Scripts/database/`. 

Common events are in `database/common_events.py`, as `@common_event`
functions of the `CommonEvents` class.

### Variables, switches and events by name

Variables and switches are attributes of `database/variables.py` and
`database/switches.py`:

```python
class Variables(VariableTable):
    class Config:
        size = 400  # slots in the editor

    deroulement_du_scenario = Variable(102, "Déroulement du scénario")
```

Event scripts import them and use them by name. Events of the same map are used by their function name:

```python
from database.common_events import common_events
from database.switches import switches
from database.variables import variables

alex_re.move(move_down * 2)                  # events[2] on this map
if switches.night_mode:                      # switches[12]
    variables.deroulement_du_scenario += 1   # variables[102]
    common_events.tombee_de_la_nuit()        # common_events[12]()
```

- **The numeric forms still work:** `events[2]`, `variables[102]`, `common_events[12]()`.
- **Use your IDE to rename variable names:** On VSCode, right click and use "Rename Symbol"

### Patches

rpgsync has advanced support for popular plugins DynRPG and Patch maniacs, so that the code is nice and clean.

The first sync detects patches from the game (`DynPlugins/`,
and the `[Patch]` section of `EasyRPG.ini`) and writes them into
`Scripts/pyproject.toml`. 

Edit them there if the detection is wrong:

```toml
[tool.rpgsync]
engine = "2003"               # "2000" | "2003": the engine checks target
patches = ["dynrpg"]          # "dynrpg" | "maniac" | "easyrpg"
allow_commands = [2055]       # single extra command codes, allowed anyway
```

EasyRPG Player reads the same `[Patch]` section of the game's `EasyRPG.ini`
(e.g. `Maniac=1`, `DynRPG=1`, `PowerMode2003=1`) when it doesn't detect a patch
by itself. See [Patches.md](Patches.md) for how each patch is handled.

## Recommended: add EasyRPG Player

[EasyRPG Player](https://easyrpg.org/player/) runs RPG Maker 2000/2003 games
on Windows, macOS, Linux and more, and is the quickest way to test your
changes: start the game, play, quit, edit, start again.

- Download: https://easyrpg.org/player/downloads/
- Run a game: `easyrpg-player --project-path /path/to/MyGame --window`
- Useful options: `--test-play` (debug mode, F9 menu), `--new-game`,
  `--start-map-id N --start-position X Y` (start right where you work).
- F12 returns to the title screen, so you can reload changed maps without
  restarting.
