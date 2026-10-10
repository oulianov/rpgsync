"""Engine compatibility checks: does the game only use what its engine supports?

RPG Maker 2000 lacks several commands and parameters of RPG Maker 2003, and
patches (Maniac Patch, EasyRPG extensions) add commands that the plain
engines do not understand.  The generated test suite of a scripts folder
runs these checks on every script, and the sync refuses to write an event
that fails them into the game.

The target engine and allowed patches are declared in the scripts'
pyproject.toml::

    [tool.rpgsync]
    engine = "2003"               # "2000" | "2003"
    patches = ["dynrpg"]          # dynrpg | maniac | easyrpg
    allow_commands = [3007]       # individual command codes
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Sequence

from . import dynparams, maniac
from .commands import CODE_NAMES
from .lcf import Command

# Commands that only exist in RPG Maker 2003
ONLY_2K3 = {1005, 1006, 1007, 1008, 1009, 5001, 5002, 5003, 5004, 5005, 13260}

# Commands whose parameter list grew in RPG Maker 2003: maximum length in 2000
MAX_PARAMS_2K = {
    10230: 5,  # Timer: no timer 2
    10710: 6,  # Battle: no condition / formation
    10810: 3,  # Teleport: no facing direction
    10860: 4,  # Set Event Location: no facing direction
    11040: 6,  # Flash Screen: no begin/end mode
    11050: 4,  # Shake Screen: no begin/end mode
    11410: 1,  # Wait: no "wait for key"
    11610: 10,  # Key Input: 2000 layout
}

PATCH_RANGES = {"easyrpg": range(2000, 3000), "maniac": range(3000, 4000)}
PATCHES = ("dynrpg", "maniac", "easyrpg")
ENGINES = {"2000": "2k", "2003": "2k3"}


def load_config(script_dir: str) -> dict:
    """The [tool.rpgsync] table of <script_dir>/pyproject.toml ({} if none)."""
    try:
        with open(os.path.join(script_dir, "pyproject.toml"), "rb") as f:
            return tomllib.load(f).get("tool", {}).get("rpgsync", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_rules(script_dir: str) -> tuple[list[str], list[int]]:
    """(patches, allow_commands) declared in <script_dir>/pyproject.toml."""
    config = load_config(script_dir)
    return list(config.get("patches", [])), list(config.get("allow_commands", []))


def target_engine(script_dir: str, detected: str) -> str:
    """Engine the checks target: `engine = "2000"` in pyproject.toml keeps an
    RPG Maker 2003 game compatible with 2000; default: the game's own."""
    declared = str(load_config(script_dir).get("engine", ""))
    if declared and declared not in ENGINES:
        raise ValueError('[tool.rpgsync] engine must be "2000" or "2003", got %r' % declared)
    return ENGINES.get(declared, detected)


def describe(code: int) -> str:
    return "%s (%d)" % (CODE_NAMES.get(code, "command"), code)


def command_problems(
    cmds: Sequence[Command], engine: str, patches: Iterable[str] = (), allow: Iterable[int] = ()
) -> list[dynparams.Problem]:
    """Problems of a command list for the given engine ("2k" or "2k3"): errors
    break the game in RPG_RT, warnings do not."""
    patches, allow = set(patches), set(allow)
    problems = []

    def add(error: bool, message: str) -> None:
        problems.append(dynparams.Problem(error=error, message=message))

    for c in cmds:
        code = c.code
        if code in allow:
            continue
        if code == 12410 and c.string.startswith(b"@") and "dynrpg" not in patches:
            if "easyrpg" in patches and c.string.startswith(b"@easyrpg_"):
                continue  # EasyRPG Player runs @easyrpg_ comments without DynRPG
            name = c.string[1:].split(b" ", 1)[0].decode("ascii", "replace")
            add(
                False,
                'DynRPG command dyn.%s() runs as a plain comment: add "dynrpg" to [tool.rpgsync] patches '
                "if the game uses DynRPG" % name,
            )
            continue
        if code not in CODE_NAMES:
            if code in maniac.CODES and "maniac" not in patches:
                add(True, 'Maniac command maniac.%s(): add "maniac" to [tool.rpgsync] patches' % maniac.name_of(code))
                continue
            if not any(code in r for p, r in PATCH_RANGES.items() if p in patches):
                patch = next((p for p, r in PATCH_RANGES.items() if code in r), None)
                hint = ' (a %s command: add "%s" to [tool.rpgsync] patches)' % (patch, patch) if patch else ""
                add(True, "unknown command %d%s" % (code, hint))
            continue
        if engine == "2k":
            if code in ONLY_2K3:
                add(True, "%s only exists in RPG Maker 2003" % describe(code))
            elif code in MAX_PARAMS_2K and len(c.params) > MAX_PARAMS_2K[code]:
                add(
                    False,
                    "%s uses RPG Maker 2003 parameters (%d, 2000 has %d): RPG Maker 2000 ignores them"
                    % (describe(code), len(c.params), MAX_PARAMS_2K[code]),
                )
    return problems + dynparams.problems(list(cmds))


def check_commands(
    cmds: Sequence[Command], engine: str, patches: Iterable[str] = (), allow: Iterable[int] = ()
) -> list[str]:
    """Problems of a command list that break the game in RPG_RT."""
    return [p.message for p in command_problems(cmds, engine, patches, allow) if p.error]


def check_warnings(specs: Sequence, engine: str, patches: Iterable[str] = (), allow: Iterable[int] = ()) -> list[str]:
    """Problems that do not break the game (see check_specs for the ones that do)."""
    out = []
    for spec in specs:
        if hasattr(spec, "pages"):
            what = "troop" if _is_troop(spec) else "event"
            lists = [("%s %d page %d" % (what, spec.id, n), page.commands) for n, page in enumerate(spec.pages, 1)]
        elif hasattr(spec, "commands"):
            lists = [("common event %d" % spec.id, spec.commands)]
        else:
            continue  # database entries have no event commands
        for where, cmds in lists:
            problems = command_problems(cmds, engine, patches, allow)
            out += ["%s: %s" % (where, p.message) for p in problems if not p.error]
    return list(dict.fromkeys(out))


def check_page_condition(condition: dict, engine: str) -> list[str]:
    problems = []
    if engine == "2k":
        if "variable" in condition and condition["variable"][1] != 1:
            problems.append("page condition: RPG Maker 2000 only supports variables[id] >= value")
        if "timer2" in condition:
            problems.append("page condition: timer2 only exists in RPG Maker 2003")
    return problems


def check_troop_page_condition(condition: dict, engine: str) -> list[str]:
    from .script import TROOP_CONDITIONS_2K3

    if engine != "2k":
        return []
    return ["page condition: %s only exists in RPG Maker 2003" % k for k in TROOP_CONDITIONS_2K3 if k in condition]


def _is_troop(spec) -> bool:
    return type(spec).__name__ == "TroopSpec"


def check_specs(specs: Sequence, engine: str, patches: Iterable[str] = (), allow: Iterable[int] = ()) -> list[str]:
    """Problems of compiled map events (EventSpec), common events (CommonEventSpec) or
    troop battle events (TroopSpec)."""
    out = []
    for spec in specs:
        if not hasattr(spec, "pages") and not hasattr(spec, "commands"):
            continue  # database entries have no event commands
        if hasattr(spec, "pages"):
            troop = _is_troop(spec)
            for n, page in enumerate(spec.pages, 1):
                where = "%s %d page %d" % ("troop" if troop else "event", spec.id, n)
                check = check_troop_page_condition if troop else check_page_condition
                out += ["%s: %s" % (where, p) for p in check(page.props["condition"], engine)]
                out += ["%s: %s" % (where, p) for p in check_commands(page.commands, engine, patches, allow)]
        else:
            out += ["common event %d: %s" % (spec.id, p) for p in check_commands(spec.commands, engine, patches, allow)]
    return list(dict.fromkeys(out))  # one line per problem and place, not per command
