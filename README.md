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
class Jeremy(Event):
    sprite = ("heroes1", 1)  # the settings of every page
    layer = "same"

    def page_1(self):
        """Greetings (stored in the game as the page's first comment)"""
        text(r"\n[3] : Bonjour, je suis \c[2]Jeremy\c[0].")
        this.move(move_up * 3, face_player, frequency=8)
        Ulysse.move(move_up * 2)  # another event of this map, by name
        if party.gold >= 100:
            match show_choices("Payer", "Partir"):
                case "Payer":
                    # Take gold (this is stored in the events!)
                    party.gold -= 100
                    items[3].count += 1
                case "Partir":
                    return
        else:
            text("Reviens avec de l'or.")
        variables[102] += 1

    @page(when=variables[102] >= 1)  # the active page: the last one whose condition holds
    def page_2(self):
        text("Merci encore !")
        Ulysse.page_1()  # runs Ulysse's page 1 (Call Event)


@event(11, "Ulysse", x=10, y=42)
class Ulysse(Event):  # The other event!
    sprite = ("heroes1", 0)
    layer = "same"

    def page_1(self):
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
Open the `Scripts` folder in your editor and select its `.venv` as the Python interpreter. You're seeing your Python clone!



### 4. Choose your editor workflow

**With the default RPG Maker 2000/2003 editor**: the best workflow is

```bash
rpgsync pull     # game -> scripts
# edit the Python files
rpgsync push     # scripts -> game
```

then open the editor and keep editing there.

> [!WARNING]
> **Keep the RPG Maker editor closed while you edit the Python files.**
> The editor keeps its own copy of your game in memory and ignores your Python
> edits: its next save (a map, the database, or quitting) writes that copy back
> over them.

**With the [EasyRPG Editor](https://github.com/oulianov/Editor)**: make sure its
**rpgsync mode** is on (Settings). The Editor reloads the changes made on disk
and shows them as well as it can, so it can stay open while `rpgsync` runs.
When both sides change the same game, it merges what each changed and only
asks when the same entry changed on both sides.

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
  `MapXXXX.conflict-<time>.py` (in one file, for a script split in files).
- The RPG Maker editor does not reload files by itself: close it while you
  edit the scripts (see [Choose your editor workflow](#4-choose-your-editor-workflow)).

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
- `--map N` limits a command to one map; `check` also takes scripts and folders of scripts
- `pull --force` overwrites scripts that have unsynced edits.
- `push` writes the scripts changed since the last sync, and overwrites the game's changes made
  in the editor; `push --force` writes every script.
- `rpgsync check` also checks the maps: teleports, vehicle locations and
  teleport / escape targets go to a map of the map tree and inside it, every
  map of the tree has its file, and no `MapNNNN.py` is left from a deleted map.

### Automated tests

The `Scripts` folder comes with tests in `Scripts/tests/`. Keep them: the
first two are also run by rpgsync itself, and **changes only go from a
script into the game if they pass**. A failing script is reported and the
game file stays as it was.

```bash
cd /path/to/MyGame/Scripts
rpgsync check
rpgsync check Map0004.py Map0012.py   # only these scripts
rpgsync check database                # only the scripts in this folder
```

`rpgsync check` also runs the type checker [ty](https://github.com/astral-sh/ty);
VS Code shows the same problems while you type, with the ty extension or
Pylance. `Scripts/pyproject.toml` turns off Pylance's warning about
`from rpgsync.dsl import *`: the scripts use it on purpose.

With `--resources`, it also checks the game's resource files (`CharSet/`,
`Picture/`, `Sound/`...) and warns about an event that uses a file the game
doesn't have (not imported, and not in the RTP when the game uses one), and
about the imported files nothing uses. Plugin comments naming a file count as
a use, and so do names completed when the game runs (a picture's number from a
variable, DynParams). A missing file gets one warning, at its first use.

A large game's scripts compile in several processes, one per CPU; `--jobs N`
sets how many (`--jobs 1`: one).

### Database, Common Events and Troop Battle Events

The database (`RPG_RT.ldb`): heroes, classes, skills, items, enemies,
troops, states, variables, switches, common events, troop battle events... is
written to `Scripts/database/`. 

Common events are in `database/common_events.py`, as `@common_event`
functions of the `CommonEvents` class.

The battle events of the troops (monster groups) are in
`database/troop_events.py`, one `@troop` function per troop that has some,
with its pages and their conditions. The troops themselves (name, monsters)
stay in `database/troops.py`:

```python
class TroopEvents(TroopEventTable):
    @troop(3, "Slime*2")
    def slime_2():
        @page(when=turn(1, every=2) and enemies[0].hp_percent(0, 50))
        def page_1(self):
            text("The slime is weak!")
            if battle_condition(enemies[1].can_act):
                enemies[1].change_hp(-10)
```

### Big scripts: a folder of files

GitLab shows no diff of a file over 200 KB. A script bigger than 160 KB
(common events, troop events, a database table, a map) is written as a folder
instead, `database/common_events/` for `database/common_events.py`:

```
database/common_events/__init__.py           the header, Config and `common_events = CommonEvents()`
database/common_events/events_0001_0021.py   class Events_0001_0021(CommonEventTable): ...
database/common_events/events_0022_0022.py   ...
```

The class of `__init__.py` is made of the classes of its files, so
`common_events.mr_clean()`, `items.potion`... work as before, in ty and VS Code
too. A map's folder (`Map0195/`) holds its events; a file imports the events of
the other files it names.

Move entries between the files, reorder them, rename the files: rpgsync keeps
every entry where it is. A new entry goes after the one before it, a file grown
over the limit is cut in two, an emptied one is removed. The first split cuts
the script in id order, in files named after their first and last ids. What is
above the class of each file, and the imports and the class statement of
`__init__.py`, are rpgsync's. rpgsync reads every `.py` file of the folder, in
the order of their names. A folder and a `.py` file for the same script is an
error.

Set the limit in `Scripts/pyproject.toml` (`0`: never split):

```toml
[tool.rpgsync]
max_file_kb = 160
```

### The map tree

The editor's map tree (`RPG_RT.lmt`) is `database/map_tree.py`: one entry per
map or area, with its map properties (music, battle background, teleport /
escape / save, random encounters). The maps under a map go in the `with` block
that follows it, so the file has the shape of the tree. `start` is where a new
game begins (the party and the vehicles).

```python
class Maps(MapTree):
    # New game: the party on Relm
    start = Start(party_map_id=1, party_x=8, party_y=6)

    hertepay = Map(id=5, name="Hertepay", music_type="event")
    with hertepay:
        relm = Map(id=1, name="Relm", encounters=[Encounter(troop_id=3)])
        with relm:
            cellar = Area(id=26, name="Cellar", area_rect=[2, 3, 9, 8])


maps = Maps()
```

- **Rename a map** with `name=`: its script's title follows.
- **Add a map** with a new `Map(name="Forest")` where it belongs in the tree:
  rpgsync gives it an id, creates an empty `MapNNNN.lmu` (20x15) and its script.
- **Delete a map** by removing its entry and its `with` block: its files and
  the scripts are deleted, after a copy in `Scripts/.rpgsync/history`.
- **Move a map** by moving its entry (and its block) elsewhere in the tree.
- Nothing goes under an area: your IDE flags `with an_area:`.

Event scripts name maps by their attribute, wherever they are in the tree:
`teleport(maps.cellar, 10, 4)` (`teleport(26, 10, 4)` still works).

### Events and pages

Each map event is a class named after it (`Gary`, `Event12` for an event
with the editor's default name), and its pages are its `page_1(self)`,
`page_2(self)`... methods, numbered like the editor's tabs:

- **The active page is the last one whose `when=` holds** (none: the event is
  not on the map), as in RPG Maker. `rpgsync check` warns about a page that can
  never be active because a page after it holds whenever it does.
- **The class attributes are the settings of every page** (`sprite`, `layer`,
  `trigger`, `move_type`...); a page's `@page(...)` states only its condition
  and what differs (`sprite=None`: no graphic). A page with no condition and
  the class's settings needs no `@page`. `direction` and `route` are set on
  each page.
- **A page's docstring is its first comment in the game**, so the editor shows
  it too.
- **`Gary.page_3()` runs Gary's page 3** (Call Event); in Gary's pages,
  `self` is Gary (`self.move(...)`, `self.page_3()`), while `this` is RPG
  Maker's "this event". Add or move pages freely:
  after the push, rpgsync names them by their number and updates the calls of
  the map. In the game, Call Event keeps a page number: when pages move in the
  editor, rpgsync points out the calls to check.

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
from database.map_tree import maps
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

#### DynRPG plugin calls

A DynRPG comment `@name args` is `dyn.name(args)` in the scripts, and
unquoted text in it a word: `dyn.remove_text("menu1", word.end)`. The
commands of the plugins rpgsync knows (DynText, DynParams, EasyRPG Player's
own...) are typed, so the editor completes them, and `rpgsync check` warns
about wrong arguments, calls that no plugin of `DynPlugins/` runs, and
`DynRPG.ini` sections no plugin reads.

#### DynParams: a variable anywhere

With the [DynParams](https://rewking.com/dynrpg/) plugin
(`DynPlugins/DynParams.dll`), write a variable wherever RPG Maker only takes a
fixed number, and a text built from variables wherever it takes a fixed text:

```python
if switches[variables.clue]:                       # the switch whose number is in a variable
    common_events[variables.next_scene]()
events[variables.npc].move(face_up, frequency=8)
pictures[variables.slot].show(f"face{variables.mood}", x=160, y=120)
variables.hp = actors[variables.hero].hp
```

rpgsync writes the `@dynparams_add_param` / `@dynparams_append_string` /
`@dynparams_overwrite_next` comments before the command for you, and only for
the values RPG Maker can't read from a variable itself (`x=variables.x` stays a
plain RPG Maker option). Pulling a game turns existing DynParams comments into
this form when it writes back the exact same commands.

Type checkers accept these variables in this game only: rpgsync writes
`Scripts/database/game_types.py`, which tells ty and Pylance that the game runs
DynParams.

The command itself still holds a fixed value, the **placeholder**: what runs
without the plugin, and what the RPG Maker editor shows. It is 1 (or `""` for a
text) unless a comment says otherwise:

```python
if switches[variables.clue]:  # placeholder: switches.relm
events[variables.npc].move(face_up)  # placeholder: this
pictures[variables.slot].erase()  # placeholder: pictures[42]
```

Some values can't go through DynParams: those RPG Maker doesn't store as the
number itself, like `wait()` (stored in tenths of a second).

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

## License

MIT, 2026: see [LICENSE](LICENSE). Source: https://github.com/oulianov/rpgsync
