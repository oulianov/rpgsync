# Writing scripts

What the scripts in `Scripts/` look like, and how to edit them. Every command, option
and value is typed and documented in [`rpgsync/dsl.py`](../rpgsync/dsl.py): your editor
shows the docs on hover and flags wrong values (a misspelled direction, a sprite index
above 7, ...).

## Events and pages

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

## Variables, switches and events by name

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

## The database, common events and troop battle events

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

## The map tree

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

## Big scripts: a folder of files

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

The events of a script are methods of one class, so a diff names that class
above each change. The set-up adds `*.py diff=python` to the `.gitattributes`
at the root of your repository: git and GitLab then name the event (GitLab
reads the default branch's).
