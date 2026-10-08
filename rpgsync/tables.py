"""What the database files import: the models of ``rpgsync.db`` plus the
table classes that hold them.

A database file is one class whose attributes are the entries::

    class Variables(VariableTable):
        class Config:
            size = 500

        code_voulu = Variable(32, "Code voulu")

    variables = Variables()

Event scripts import ``variables`` / ``switches`` from these files, so
``variables.code_voulu`` reads and writes like ``variables[32]`` (and
``variables[32]`` keeps working).  The attribute names belong to the
scripts: renaming a variable in the editor keeps them, and an IDE rename
changes nothing in the game.

The files are parsed by rpgsync, never run; the classes here only exist so
that editors and type checkers understand them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, overload

from . import db, dsl
from .db import *  # noqa: F403

__all__ = [*db.__all__, "SwitchTable", "Table", "VariableTable"]


class Table:
    """A database table: its attributes are the entries.  The nested
    ``class Config:`` holds ``size``, the number of slots the editor shows."""


class VariableTable(dsl.Variables, Table):
    """The variables of the database (``variables[32]`` works too)."""


class SwitchTable(dsl.Switches, Table):
    """The switches of the database (``switches[12]`` works too)."""


class Variable(db.Variable):
    """A named variable: ``code_voulu = Variable(32, "Code voulu")``.  Event
    scripts use it as ``variables.code_voulu``."""

    def __init__(self, id: int | None = None, name: str = "") -> None:
        super().__init__(id=id, name=name)

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, owner: type, /) -> Self: ...
        @overload
        def __get__(self, obj: object, owner: type, /) -> dsl.Variable: ...
        def __get__(self, obj: object, owner: type, /) -> Self | dsl.Variable: ...
        def __set__(self, obj: object, value: dsl.Number, /) -> None: ...


class Switch(db.Switch):
    """A named switch: ``night_mode = Switch(12, "Night mode")``.  Event
    scripts use it as ``switches.night_mode``."""

    def __init__(self, id: int | None = None, name: str = "") -> None:
        super().__init__(id=id, name=name)

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, owner: type, /) -> Self: ...
        @overload
        def __get__(self, obj: object, owner: type, /) -> dsl.Switch: ...
        def __get__(self, obj: object, owner: type, /) -> Self | dsl.Switch: ...
        def __set__(self, obj: object, value: bool, /) -> None: ...
