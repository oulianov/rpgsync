"""The two ways a standard command can be stored: RPG Maker's and the Maniac Patch's.

The Maniac Patch editor stores some standard commands with other parameters than
RPG Maker's editor, for the same meaning: a Control Variables of one variable
keeps 0 as its last id and drops the unused last parameter, a Wait keeps the
value selector of its duration, a Call Event of a common event has no page::

    Control Variables: V[0001] = 456     RPG Maker [0, 1, 1, 0, 0, 456, 0]
                                         Maniac    [0, 1, 0, 0, 0, 456]

A script writes both the same way, ``variables[1] = 456``.  Each game stores
most of its commands one way (detected once, see detect_maniac_layout); a
statement stored the other way says so in a comment at the end of its line::

    variables[1] = 456  # layout: maniac
    variables[2] = 7  # layout: rpg_maker

The comment changes how the command is stored, not what it does: dropping it
stores the command the game's usual way.  Encoders build the RPG Maker form;
to_maniac() turns it into the Maniac one, to_standard() the other way.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Callable, Iterable

from .lcf import Command, LcfError, LcfFile

MANIAC = "maniac"
RPG_MAKER = "rpg_maker"
LAYOUTS = (MANIAC, RPG_MAKER)

_COMMENT = re.compile(r"^#\s*layout:\s*(\w+)")


def statement_layout(lines: list[str]) -> str | None:
    """The `# layout: ...` comment at the end of a statement's line (its last line, for a
    statement over several lines), None without one.  ValueError for an unknown layout."""
    src = "\n".join(lines) + "\n"
    if "layout" not in src:
        return None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and tok.line[: tok.start[1]].strip():
                m = _COMMENT.match(tok.string)
                if m is not None:
                    if m.group(1) not in LAYOUTS:
                        raise ValueError("unknown layout %r (layout: maniac or layout: rpg_maker)" % m.group(1))
                    return m.group(1)
    except (tokenize.TokenError, SyntaxError):
        return None
    return None


def comment(layout: str) -> str:
    return "  # layout: " + layout


# --------------------------------------------------------------------------
# Command by command: RPG Maker form <-> Maniac form
# --------------------------------------------------------------------------

# Control Variables operands whose Maniac form always has the selector of their value (p7)
_CV_SELECTED = {3, 4, 5, 6, 8}
# ... and those whose Maniac form drops the unused p6
_CV_SHORT = {0, 1, 2, 7}


def _cv_maniac(p: list[int]) -> list[int] | None:
    if len(p) < 7:
        return None
    out = list(p)
    if out[0] == 0:
        if out[2] != out[1]:
            return None
        out[2] = 0
    elif out[0] == 2 and out[2] != 0:
        return None
    if out[4] in _CV_SHORT:
        if len(out) != 7 or out[6] != 0:
            return None
        out = out[:6]
    elif out[4] in _CV_SELECTED and len(out) == 7:
        out.append(0)
    return out


def _cv_standard(p: list[int]) -> list[int] | None:
    if len(p) < 6:
        return None
    out = list(p)
    if out[0] == 0:
        if out[2] != 0:
            return None
        out[2] = out[1]
    elif out[0] == 2 and out[2] != 0:
        return None
    if out[4] in _CV_SHORT:
        if len(out) != 6:
            return None
        out.append(0)
    elif out[4] in _CV_SELECTED and len(out) == 8 and out[7] == 0:
        out = out[:7]  # (a value given by a variable keeps its selector in both forms)
    elif out[4] in _CV_SELECTED and len(out) != 8:
        return None
    return out


def _cs_maniac(p: list[int]) -> list[int] | None:
    if len(p) != 4 or p[0] != 0 or p[2] != p[1]:
        return None
    return [p[0], p[1], 0, p[3]]


def _cs_standard(p: list[int]) -> list[int] | None:
    if len(p) != 4 or p[0] != 0 or p[2] != 0 or p[1] == 0:
        return None
    return [p[0], p[1], p[1], p[3]]


def _padded(standard: int, extra: int) -> tuple[Callable, Callable]:
    """A command the Maniac editor writes with `extra` more zero parameters."""

    def maniac(p: list[int]) -> list[int] | None:
        return list(p) + [0] * extra if len(p) == standard else None

    def rpg_maker(p: list[int]) -> list[int] | None:
        if len(p) != standard + extra or any(p[standard:]):
            return None
        return list(p[:standard])

    return maniac, rpg_maker


def _call_event_maniac(p: list[int]) -> list[int] | None:
    if len(p) == 3 and p[0] == 0 and p[2] == 0:
        return p[:2]  # a common event: no page
    return None


def _call_event_standard(p: list[int]) -> list[int] | None:
    if len(p) == 2 and p[0] == 0:
        return [0, p[1], 0]
    return None


def _trimmed(standard: int) -> tuple[Callable, Callable]:
    """A command the Maniac editor writes without its trailing zero parameters."""

    def maniac(p: list[int]) -> list[int] | None:
        out = list(p)
        while out and out[-1] == 0:
            out.pop()
        return out

    def rpg_maker(p: list[int]) -> list[int] | None:
        if len(p) > standard:
            return list(p) if any(p[standard:]) and p[-1] != 0 else None  # Maniac's own parameters
        return list(p) + [0] * (standard - len(p))

    return maniac, rpg_maker


# code -> (RPG Maker form -> Maniac form, Maniac form -> RPG Maker form): None when the
# command has no such form
FORMS: dict[int, tuple[Callable[[list[int]], list[int] | None], Callable[[list[int]], list[int] | None]]] = {
    10220: (_cv_maniac, _cv_standard),  # Control Variables
    10120: _padded(4, 7),  # Message Options: window size, font, spacing
    10130: _trimmed(3),  # Change Face Graphic
    11910: _padded(0, 2),  # Open Save Menu (Call System Functions: save menu)
    10210: (_cs_maniac, _cs_standard),  # Control Switches
    11410: _padded(2, 1),  # Wait: the value selector of the duration
    10610: _padded(1, 2),  # Change Hero Name: the selectors, the string variable
    10620: _padded(1, 2),  # Change Hero Title
    12320: _padded(0, 3),  # Erase Event: the selector, erase / return, the event (0: this one)
    12220: _padded(0, 2),  # Break Loop: break / continue, levels
    12210: _padded(0, 5),  # Loop (infinite): kind, selectors, values, index variable
    22210: _padded(0, 5),  # End Loop: the same
    12330: (_call_event_maniac, _call_event_standard),  # Call Event of a common event
}


def to_maniac(c: Command) -> Command | None:
    """The Maniac form of a command in RPG Maker's form, None when it has none."""
    form = FORMS.get(c.code)
    p = form[0](c.params) if form else None
    return None if p is None else Command(code=c.code, indent=c.indent, string=c.string, params=p)


def to_standard(c: Command) -> Command | None:
    """RPG Maker's form of a command in the Maniac form, None when it is not one."""
    form = FORMS.get(c.code)
    p = form[1](c.params) if form else None
    return None if p is None else Command(code=c.code, indent=c.indent, string=c.string, params=p)


def stored(c: Command, layout: str) -> Command:
    """A command compiled in RPG Maker's form, as stored in `layout`."""
    if layout == MANIAC:
        return to_maniac(c) or c
    return c


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def _votes(cmds: Iterable[Command], votes: dict[str, int]) -> None:
    for c in cmds:
        p = c.params
        if c.code in (10210, 10220) and len(p) >= 4 and p[0] == 0 and p[1] != 0:
            if p[2] == 0:
                votes[MANIAC] += 1
            elif p[2] == p[1]:
                votes[RPG_MAKER] += 1


def detect_maniac_layout(map_paths: Iterable[str], ldb_path: str | None = None, max_maps: int = 60) -> bool:
    """Are most Control Variables / Control Switches of one variable stored the Maniac
    way (0 as last id)?  Counted over the common events and up to `max_maps` maps spread
    over the game (its first maps may predate the patch)."""
    votes = {MANIAC: 0, RPG_MAKER: 0}
    paths = list(map_paths)
    if len(paths) > max_maps:
        paths = [paths[i * len(paths) // max_maps] for i in range(max_maps)]
    try:
        if ldb_path is not None:
            for it in LcfFile.load(ldb_path).root.get("commonevents"):
                _votes(it.struct.get("event_commands"), votes)
                it.struct.release()
        for path in paths:
            for ev in LcfFile.load(path).root.get("events"):
                for page in ev.struct.get("pages"):
                    _votes(page.struct.get("event_commands"), votes)
    except (OSError, LcfError):
        pass
    return votes[MANIAC] > votes[RPG_MAKER]
