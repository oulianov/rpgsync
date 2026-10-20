# DynRPG and DynParams

rpgsync has advanced support for DynRPG plugins, so that the code stays clean.
[Patches.md](../Patches.md) has the details of every patch rpgsync handles, and the
`[tool.rpgsync]` settings of `Scripts/pyproject.toml` that allow them.

## DynRPG plugin calls

A DynRPG comment `@name args` is `dyn.name(args)` in the scripts, and
unquoted text in it a word: `dyn.remove_text("menu1", word.end)`. The
commands of the plugins rpgsync knows (DynText, DynParams, EasyRPG Player's
own...) are typed, so the editor completes them, and `rpgsync check` warns
about wrong arguments, calls that no plugin of `DynPlugins/` runs, and
`DynRPG.ini` sections no plugin reads.

## DynParams: a variable anywhere

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
