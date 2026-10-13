# Spec: map events as classes

Status: implemented (2026-10-13), with the changes listed in "Changes from the draft"
at the end.

## Goal

Make map event scripts read like Python while staying an exact twin of the game:

- one class per event, with the settings its pages share as class attributes;
- pages are `page_N` functions, `N` being the page number of the editor's tabs;
- `@page(...)` only when a page has a condition or a setting of its own;
- a page's label is its docstring, stored in the game as the page's first comment;
- pages are called by name: `Gary.page_3()`;
- rpgsync warns about pages that can never be active.

`this` keeps its meaning (the event running the code: the caller, in a common
event). Common events and troop events are out of scope (see the end).

## Before / after

A real event (Hertepay, `Map0011.py`), today:

```python
@event(30, "Gary", x=17, y=16)
def gary():
    @page(when=variables.deroulement_du_scenario == 5, sprite=("garywiss", 0), layer="same")
    def page_1():
        pass

    @page(when=variables.deroulement_du_scenario == 8, sprite=("garywiss", 0), layer="same")
    def page_2():
        pass

    @page(when=variables.deroulement_du_scenario == 9, sprite=("garywiss", 0), layer="same")
    def page_3():
        text(r"\N[9] : Vous vous trompez !")
        ...

    @page(when=variables.deroulement_du_scenario == 18, sprite=("garywiss", 0), layer="same")
    def page_4():
        pass

    @page(when=variables.deroulement_du_scenario == 19, sprite=("garywiss", 0), layer="same")
    def page_5():
        ...

    @page(when=switches.gary_arrete)
    def page_6():
        pass
```

After:

```python
@event(30, "Gary", x=17, y=16)
class Gary(Event):
    sprite = ("garywiss", 0)
    layer = "same"

    @page(when=variables.deroulement_du_scenario == 5)
    def page_1(self):
        pass

    @page(when=variables.deroulement_du_scenario == 8)
    def page_2(self):
        pass

    @page(when=variables.deroulement_du_scenario == 9)
    def page_3(self):
        text(r"\N[9] : Vous vous trompez !")
        ...

    @page(when=variables.deroulement_du_scenario == 18)
    def page_4(self):
        pass

    @page(when=variables.deroulement_du_scenario == 19)
    def page_5(self):
        ...

    @page(when=switches.gary_arrete, sprite=None, layer="below")
    def page_6(self):
        pass
```

A one-page NPC, whose page starts with a comment in the game:

```python
@event(10, "Jeremy", x=15, y=38)
class Jeremy(Event):
    sprite = ("heroes1", 1)
    layer = "same"

    def page_1(self):
        """Greets the player, sells the map"""
        text(r"\n[3] : Bonjour, je suis \c[2]Jeremy\c[0].")
        if party.gold >= 100:
            party.gold -= 100
```

## The format

### Event class

```python
@event(id, "Name", x=X, y=Y)
class Name(Event):
    <settings>        # optional: name = value
    <pages>           # def page_N(): ..., in page order
```

- `@event(...)` is unchanged: id (left out for a new event, assigned and written
  back as today), name, x, y.
- The base class is `Event`. The class name is the event's handle, as the function
  name is today (`Gary.move(...)`, `ev_12` for events with a generic name). It stays
  lowercase: it is used as an object, never instantiated.
- The body holds only settings (assignments), page functions and `pass`. A class
  docstring is an error: RPG Maker events have no description ("write it as the
  docstring of page_1, or as a # comment in a page").
- An event without pages: `pass`.

### Settings on the class

The attributes a class may set are the page settings:

`sprite`, `tile`, `pattern`, `translucent`, `trigger`, `layer`,
`overlap_forbidden`, `animation`, `move_type`, `frequency`, `speed`.

Not on the class: `when` and the condition slots (`switch_a`, `switch_b`,
`variable`, `item`, `actor`, `timer`, `timer2`), `id`, `raw`: they belong to one page;
and `direction`, `route` (see "Changes from the draft").

Meaning: a page's setting is its `@page(...)` value if given, else the class's,
else RPG Maker's default (the defaults of `page()` today).

The graphic is one setting: `sprite=(...)`, `tile=N` or none. A page's `sprite=` or
`tile=` replaces the class's graphic; `sprite=None` (or `tile=None`) means no graphic.

Writing (decompile): for each setting, the class takes the value most pages have
(ties: the earliest page's value), unless that value is RPG Maker's default. A page
then states only the settings that differ from the class. With one page, every
setting of the page goes on the class.

### Pages

- A page is a function `page_N` of the class, `N` its number: its position among the
  pages (1, 2, 3...), as in the editor's tabs and as Call Event counts them (EasyRPG
  Player: `Game_Event::GetPage(page)` returns `pages[page - 1]`).
- `@page(...)` is written only when the page has a condition (`when=`) or a setting
  that differs from the class's. A function `page_N` without decorator is a page with
  no condition and the class's settings.
- `@page` keeps its keyword arguments; `when=` stays a keyword.
- Pages whose stored id differs from their position (hand-made data: the map scripts
  of TestGame-2003 and Hertepay have none) keep `@page(id=K)` as today.
- Page names are the numbers. A page added in the script may have any name
  (`def accusation(self):`); after the push, rpgsync renames every page function to
  `page_<position>` and updates the calls to it in the same script
  (`Gary.accusation()` -> `Gary.page_3()`), and says so in the sync output
  ("Map0011: gary: pages renamed by their number: accusation -> page_3").
- Two page functions with the same name: error (Python would keep only the last).

### Page labels: docstrings

- A page's docstring is its first comment in the game: Comment commands (12410, then
  22410 for each further line) at the start of its command list. It shows on hover in
  the IDE (`Gary.page_3`), and as the first line of the page in RPG Maker.
- Compile: a string as the first statement of a page compiles to those Comment
  commands. The text is cleaned like `inspect.cleandoc`; its first line is the 12410
  command, the others 22410. A page whose only statement is its docstring needs no
  `pass`.
- Decompile: a page whose commands start with a plain comment gets it as its
  docstring when compiling the docstring gives back exactly the same commands
  (otherwise, and for `"""` inside, backslashes, leading or trailing blanks, the
  comment stays as `#` lines, as today). Comments that are commands are never
  docstrings: DynRPG (`@...`) and DynParams (`@dynparams_...`) comments.
- A string statement elsewhere in a page is an error ("a string alone does nothing:
  text(...) shows a message, # writes a comment"). Today such strings are ignored.

### Calls to pages

- `Gary.page_3()` is Call Event (map event: `gary`, page 3), params `[1, 30, 3]`. The
  event is one of this map (Call Event only reaches the current map's events).
- In the script, a call names a page function: it runs that page wherever the page
  ends up after an edit of the script (a moved page takes its calls with it; rpgsync
  rewrites the call with the new number, see Pages).
- In the game, Call Event holds a number, as in RPG Maker: when pages are inserted,
  removed or moved in the editor, the calls keep their number. rpgsync then warns
  (best effort, comparing with the last synced script): "Map0011.py:40:
  Gary.page_3() now runs another page: gary's pages changed in the editor".
- Decompile writes `name.page_N()` when the target is a named event of this map with
  at least N pages; otherwise as today (`events[30].call(3)`, `name.call(9)`).
- Unchanged: `this.call(3)` (`this` is any event), `events[30].call(3)`, the
  variables form, `Gary.call(3)` (still accepted).

### Which page is active: unreachable pages

The active page is the last page whose conditions hold; if none holds, the event is
absent. `@page`'s docs say so (as today); rpgsync also reports pages that can never be
active:

- Page P is never active when a page Q after it holds whenever P holds: every
  condition of Q is implied by P's conditions.
  - Q has no condition: it shadows every page before it.
  - switch, item, actor: the same condition in P.
  - variable: same variable, and the values P accepts are a subset of Q's
    (`== 5` implies `>= 3`; `>= 5` implies `>= 3`; 2003 comparisons as intervals).
  - timer `<= a` in P implies timer `<= b` in Q when `a <= b` (same timer).
- Message: "Map0011.py:12: Gary.page_2 is never active: page_5, after it, holds
  whenever page_2 does (no condition)".
- Reported by `rpgsync check` as a warning, and by a scaffold test
  `test_pages_reachable` that does not gate the sync. Not an error: RPG Maker allows
  it, and a page kept aside on purpose is common.

## Type stubs (`rpgsync/dsl.py`)

Checked with ty on a prototype (the good file passes; the bad one gets the six
errors below):

```python
class _EventType(Character, type):
    """What an event class is: a character of the map (Gary.move(...))."""

class Event(metaclass=_EventType):
    """A map event: its pages are its page_N functions; its attributes are the
    settings of every page, unless a page's @page(...) says otherwise."""
    sprite: tuple[str, SpriteIndex] | None = None
    tile: int | None = None
    trigger: Trigger = "action"
    layer: Layer = "below"
    ...  # every class setting, typed as page()'s keyword

E = TypeVar("E", bound=type[Event])
_F = TypeVar("_F", bound=Callable[[], None])

def event(id: int | None = None, name: str = "", x: int = 0, y: int = 0) -> Callable[[E], E]: ...
def page(*, when: Condition | bool | None = None, ...) -> Callable[[_F], _F]: ...
```

- `event` returns the class itself, so `Gary.page_3` and `Gary.move` resolve.
- `page` returns the function itself: `Gary.page_3()` takes no argument and hovers
  with the page's docstring.
- A page function without `self` is fine: pages are reached on the class.
- Prototype results: `Gary.move(...)`, `Gary.page_1()` (no decorator),
  `Gary.page_2()`, `Gary.call(2)`, `this.call(1)`, a call to an event defined further
  down the file: all pass. Flagged: `sprite = ("garywiss", 9)`, `layer = "middle"`,
  `@page(trigger="talk")`, `Gary.page_4()` (no such page), `Gary.page_2(1)`,
  `Gary.nope(...)`.

## Code changes

- `script.py`
  - `decompile_event`: class header, settings factored onto the class, `page_N`
    without decorator when nothing to say, docstrings from leading comments.
  - `_compile_map_statements`: read the class form (settings, pages, docstrings,
    errors above); keep reading the function form (migration).
  - `_event_function_ids`, `split_entries`, `_entry_function_names`,
    `parse_by_entries`: entries start with `@event(...)` + `class Name(Event):`
    (incremental sync by entry keeps working).
  - The leading docstring of a page -> Comment commands; stray strings -> error.
- `pyexpr.py`: `name.page_N()` -> Call Event; decompile of Call Event to
  `name.page_N()`.
- `dsl.py`: `Event`, `_EventType`, generic `event`, identity `page`.
- `sync.py`: FORMAT 7; after a push, rename page functions to their number and update
  the calls of the same script (like `insert_ids`); the editor-side renumbering
  warning.
- `cli.py`: `locate --map M --event E --page P` points at the `def page_P` line, as
  today (the Editor's "Open in Script Editor" uses it: no Editor change).
- `checks`: unreachable pages (`rpgsync check`), scaffold `test_pages_reachable`.
- README, `@page` / `Event` docs, scaffold test docs.

## Migration

- FORMAT 7: scripts without unsynced edits are regenerated once in the class form.
- A script with unsynced edits in the function form still compiles; rpgsync pushes it,
  then writes it back in the class form (as for the flat map_tree.py).
- Calls `Gary.call(3)` in existing scripts are rewritten `Gary.page_3()` by the
  regeneration (same commands in the game).

## Out of scope

- Troop events (`@troop` functions with `@page`): their pages follow another rule
  (each page runs when its condition holds) and stay as they are. The same
  "no decorator when nothing to say" could follow later.
- Common events: no pages.
- `this`, `events[...]`, the conditions syntax.

## Tests

- Round trip of every test game and of the scratch Hertepay copy: export, compile,
  same bytes.
- Factoring: majority value on the class, ties, defaults left out, one-page events,
  graphic replaced by `tile=`/`sprite=None`.
- No decorator: `def page_N()` alone; decorator only for conditions and differing
  settings.
- Docstrings: one line, several lines, a comment that cannot be a docstring stays
  `#`, DynRPG / DynParams comments stay commands, a stray string is an error.
- Page names: a new page with any name renamed after the push, calls in the same
  script updated; duplicate names refused; a moved page takes its calls.
- Calls: `Gary.page_3()` <-> `[1, 30, 3]`; `this.call(3)` unchanged; a call to a
  missing page reported; the editor-side renumbering warning.
- Unreachable pages: each implication rule, and no false positive on Gary.
- Migration: function form with unsynced edits pushed then rewritten.
- ty: the generated scripts of the test games pass `rpgsync check`.

## Decisions to confirm

1. Page names: rename any name to `page_N` after a push (this spec), or refuse names
   that are not `page_<position>`?
2. Class settings: the most common value (this spec), or only values shared by every
   page that has a graphic (more stable when a page is added, longer pages)?
3. Unreachable pages: warning (this spec) or error?
4. A stray string in a page: error (this spec), or keep ignoring it?

## Changes from the draft

- `direction` and `route` are not class settings; they stay on the pages.
  `route = route(...)` in a class body would shadow the `route()` function there, where
  `@page(route=route(...))` is written. `gary.direction` is where the event faces now (a
  character's attribute, a `Value`): ty checks `direction = "left"` in an event class
  against it.
- The unreachable-page check leaves out the pages the map runs with Call Event
  (`gary.page_1()`, `this.call(1)` in the event itself): Call Event runs a page whatever
  its conditions, a common way to keep "subroutine" pages. A map with a Call Event by
  variables (`events[variables[1]].call(variables[2])`) is not checked.
- Decisions taken as proposed: pages of any name are renamed `page_N` after a push;
  the class takes the most common value (ties: the earliest page's); unreachable pages
  are warnings; a stray string in a page is an error.
- Implemented in `rpgsync/script.py` (format, compile, `rename_pages`,
  `renumbered_page_calls`, `unreachable_pages`), `pyexpr.py` (page calls), `dsl.py`
  (`Event`), `sync.py` (FORMAT 7, `MapUnit.after_import` / `export_warnings`), `cli.py`
  (`check`), `scaffold.py` (`test_pages_reachable`); tests in
  `tests/test_event_classes.py`.
- Class names are in CapWords (`class Gary(Event):`, `HaruIntro`, `Event12` for an event
  with the editor's default name) and pages take `self`: `def page_1(self):`. Pyright
  (Pylance) reports a page without `self` (reportSelfClsParameterName) and refuses
  `Gary.page_1()` on a function without `self` ("Could not bind method"); with `self`,
  `@event` types the event as an object of its class, and both pyright and ty accept
  `Gary.page_1()` and `Gary.move(...)`. In a page, `self` is the event, written by its id
  (the decompiler writes `self` for the event's own id in its pages); `this` stays RPG
  Maker's "this event" (10005). Scripts in the earlier form (lowercase classes, pages
  without `self`) are rewritten once (FORMAT 8).
