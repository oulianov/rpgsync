# rpgsync

A Python twin of your RPG Maker 2000/2003 game.

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
                    # Take gold (this is stored in the events!)
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
[`rpgsync/dsl.py`](https://github.com/oulianov/rpgsync/blob/master/rpgsync/dsl.py): your
editor shows the docs on hover and flags wrong values (a misspelled direction, a sprite
index above 7, ...). [Writing scripts](https://github.com/oulianov/rpgsync/blob/master/docs/scripts.md)
explains events and pages, variables by name, the database and the map tree.

## Demo

A Doom-like 3d game in RPGMaker using only pictures of vertical slices. The tedious, repetitive event code has been automatically generated.

https://github.com/user-attachments/assets/9246ff34-b226-4ebc-b8a3-e55b3adcc5d5


## Compatibility

rpgsync works with **RPG Maker 2000 and 2003** games, both in old versions (1.08) and
Steam version (1.12a). It was tested on EasyRPG's test suites and on complete games.
[Learn more.](https://github.com/oulianov/rpgsync/blob/master/Tested_Games.md)

**Supported Patches:** Everything! DynRPG, Maniac Patch, EasyRPG commands, Power Mode
2003, Ineluki Key Patch... Patches with unknown commands are kept as raw. Patches that
only change the engine don't matter for this module.


## Getting started

### 1. Install uv

rpgsync uses [uv](https://docs.astral.sh/uv/) to manage Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)

### 2. Install rpgsync

```bash
uv tool install rpgsync
```

This puts the `rpgsync` command on your PATH.

### 3. Set up your game

Run rpgsync in your game folder (the one containing `RPG_RT.ldb`):

```bash
cd /path/to/MyGame
rpgsync
```

The first time, it asks, then writes your maps, database and common events as Python
scripts in `Scripts/`, with tests and their own Python environment. It then keeps the
game and the scripts in sync until Ctrl+C.

Open the `Scripts` folder in your editor and select its `.venv` as the Python
interpreter. You're seeing your Python clone!

If the game is in git, commit `Scripts/` with it, `pyproject.toml` and `uv.lock`
included: they pin the rpgsync version of the game (see
[Sharing a game](#sharing-a-game)). Its `.gitignore` leaves out `.venv/` and rpgsync's
own files (`.rpgsync/`).


## Everyday use

```bash
rpgsync           # keep the game and the scripts in sync, until Ctrl+C
rpgsync status    # what is pending, on which side
rpgsync pull      # game -> scripts
rpgsync push      # scripts -> game
rpgsync check     # compile and check the scripts (types too), write nothing
rpgsync clean     # delete the backups of overwritten files
```

- A command works on the game of the current folder (the game's or `Scripts/`), or of
  the folder given: `rpgsync status /path/to/MyGame`.
- `--map N` limits a command to one map. `check` also takes scripts and folders:
  `rpgsync check Map0004.py database`.
- `pull --force` overwrites scripts that have unsynced edits; `push --force` writes
  every script, not only those changed since the last sync.
- `rpgsync locate --map 4 --event 12` prints where the scripts write an event
  (`FILE:LINE`).
- `rpgsync COMMAND --help` lists a command's options.

### With the EasyRPG Editor

> [!TIP]
> **The EasyRPG Editor is the recommended editor to use with rpgsync.** It stays open
> while rpgsync runs, and merges your Python edits with the changes you make in it.

In the [EasyRPG Editor](https://github.com/oulianov/Editor), turn on **rpgsync mode**
(Settings). The Editor reloads the changes made on disk and shows them as well as it
can, so it can stay open while `rpgsync` runs. When both sides change the same game,
it merges what each changed and only asks when the same entry changed on both sides.

### With the RPG Maker 2000/2003 editor

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

### Play-test with EasyRPG Player

[EasyRPG Player](https://easyrpg.org/player/) runs RPG Maker 2000/2003 games on
Windows, macOS, Linux and more, and is the quickest way to test your changes: start
the game, play, quit, edit, start again.

### Will this break my project?

rpgsync is built so that **it should not**. If anything, you can always go back.

- **Invalid scripts are never written.** A typo, a wrong value or a RPG Maker 2003
  command in a 2000 game stops at the
  [checks](https://github.com/oulianov/rpgsync/blob/master/docs/checks.md).
- **Every overwritten file is backed up** (game files and scripts) in
  `Scripts/.rpgsync/history/`, the last 50 syncs. `rpgsync clean` deletes them once
  you no longer need them.
- Only what you changed is written.
- If the same event changed on both sides, the editor's version is kept and your
  script is saved next to it as `MapXXXX.conflict-<time>.py` (in one file, for a
  script split in files).

Using git on the game folder and doing backups is still a good idea.


## Sharing a game

`Scripts/pyproject.toml` depends on the rpgsync release that set the game up
(`rpgsync~=0.1.0`), and `Scripts/uv.lock` pins its exact version. Whoever clones the
game runs that version, in `Scripts/`:

```bash
uv sync           # install the game's rpgsync, and the scripts' tools
uv run rpgsync    # run it
```

A CI job does the same: `uv sync --locked`, then `uv run --locked rpgsync ...`.


## Upgrading rpgsync

The `rpgsync` command:

```bash
uv tool upgrade rpgsync
```

A game, in its `Scripts/` folder:

- **A fix release** (0.1.0 → 0.1.3) writes the same scripts:

  ```bash
  uv sync --upgrade-package rpgsync
  ```

  then commit `uv.lock`.
- **A new minor version** (0.1.x → 0.2.0) writes the scripts differently. With
  nothing pending (`rpgsync status`):

  ```bash
  uv add "rpgsync~=0.2.0"
  uv run rpgsync pull --force
  ```

  then commit `pyproject.toml`, `uv.lock` and the rewritten scripts together.

[CHANGELOG.md](https://github.com/oulianov/rpgsync/blob/master/CHANGELOG.md) lists
what each version changes. An `rpgsync` command newer than a game's version would
rewrite its scripts in the newer format: on a game you don't upgrade yet, use
`uv run rpgsync` in its `Scripts/`.


## Learn more

- [Writing scripts](https://github.com/oulianov/rpgsync/blob/master/docs/scripts.md):
  events and pages, variables and switches by name, the database, the map tree, big
  scripts as folders
- [Checks and tests](https://github.com/oulianov/rpgsync/blob/master/docs/checks.md):
  `rpgsync check`, the scripts' tests, resource files
- [DynRPG and DynParams](https://github.com/oulianov/rpgsync/blob/master/docs/dynrpg.md):
  plugin calls, and a variable wherever RPG Maker takes a number
- [Patches](https://github.com/oulianov/rpgsync/blob/master/Patches.md): how each patch
  shows in the scripts, and their settings
- [Tested games](https://github.com/oulianov/rpgsync/blob/master/Tested_Games.md)
- [Working on rpgsync](https://github.com/oulianov/rpgsync/blob/master/docs/development.md):
  use your own rpgsync in a game, run its tests, release it
- [Changelog](https://github.com/oulianov/rpgsync/blob/master/CHANGELOG.md)


## License

MIT, 2026: see [LICENSE](https://github.com/oulianov/rpgsync/blob/master/LICENSE). Source: https://github.com/oulianov/rpgsync
