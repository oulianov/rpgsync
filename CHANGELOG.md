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
