# Checks and tests

## The scripts' tests

The `Scripts` folder comes with tests in `Scripts/tests/` (its `README.md` lists
them). Keep them: the first two are also run by rpgsync itself, and **changes only
go from a script into the game if they pass**. A failing script is reported and the
game file stays as it was. Run them all with `uv run pytest` in `Scripts/`, and add
your own tests next to them.

## `rpgsync check`

`rpgsync check` compiles and checks the scripts, and writes nothing:

```bash
cd /path/to/MyGame/Scripts
rpgsync check
rpgsync check Map0004.py Map0012.py   # only these scripts
rpgsync check database                # only the scripts in this folder
```

- **Engine and patches:** no RPG Maker 2003 command in a 2000 game, no patch command
  the game doesn't declare (see [Patches.md](../Patches.md)).
- **Maps:** teleports, vehicle locations and teleport / escape targets go to a map
  of the map tree and inside it, every map of the tree has its file, and no
  `MapNNNN.py` is left from a deleted map.
- **Pages:** a warning for an event page that can never be active.
- **Types:** it runs the type checker [ty](https://github.com/astral-sh/ty); VS Code
  shows the same problems while you type, with the ty extension or Pylance.
  `Scripts/pyproject.toml` turns off Pylance's warning about
  `from rpgsync.dsl import *`: the scripts use it on purpose.

With `--resources`, it also checks the game's resource files (`CharSet/`,
`Picture/`, `Sound/`...) and warns about an event that uses a file the game
doesn't have (not imported, and not in the RTP when the game uses one), and
about the imported files nothing uses. Plugin comments naming a file count as
a use, and so do names completed when the game runs (a picture's number from a
variable, DynParams). A missing file gets one warning, at its first use.

A large game's scripts compile in several processes, one per CPU; `--jobs N`
sets how many (`--jobs 1`: one).
