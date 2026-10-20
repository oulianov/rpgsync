"""Turn a script folder into a small uv project (IDE support, tests, linting)."""

from __future__ import annotations

import configparser
import json
import os
import re
import tomllib
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

from .project import Project

RPGSYNC_ROOT = Path(__file__).resolve().parent.parent
RPGSYNC_GIT = "https://github.com/oulianov/rpgsync"
# The scripts project finds rpgsync through a link, in the folder rpgsync keeps out of
# git, to this computer's rpgsync folder: pyproject.toml and uv.lock then hold the
# same relative path on every computer sharing the game.
RPGSYNC_LINK = ".rpgsync/rpgsync"
LINK_SOURCE = (
    "# .rpgsync/rpgsync: a link to this computer's rpgsync folder, made by rpgsync\n"
    f'rpgsync = {{ path = "{RPGSYNC_LINK}", editable = true }}'
)
# the source line of an older rpgsync: the absolute path of its folder on one computer
_PATH_SOURCE = re.compile(r'^rpgsync = \{ path = "(?P<path>[^"]*)", editable = true \}[ \t]*$', re.M)

PYRIGHT = """\
[tool.pyright]
# Pylance: the scripts use `from rpgsync.dsl import *` on purpose
reportWildcardImportFromLibrary = false
"""

PYPROJECT = (
    """\
[project]
name = "{name}"
version = "0.1.0"
description = "Event scripts of {title}, kept in sync with the game by rpgsync"
requires-python = ">=3.11"
dependencies = ["rpgsync"]

[dependency-groups]
dev = ["pytest>=8", "pytest-xdist>=3", "ruff>=0.6", "ty"]

[tool.uv]
package = false

[tool.uv.sources]
{source}

[tool.rpgsync]
# game folder, relative to this file
game = "{game}"
# RPG Maker version the game must run on: "2000" or "2003" (detected from
# RPG_RT.ldb). Set "2000" on a 2003 game to keep it 2000-compatible.
engine = "{engine}"
# engine patches whose extra commands the game may use: "dynrpg", "maniac", "easyrpg"
patches = {patches}
# individual extra command codes allowed anyway
allow_commands = []
# a script bigger than this (KB) is written as a folder of files instead, as GitLab
# shows no diff of a file over 200 KB (0: never)
max_file_kb = 160

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-n auto"  # pytest-xdist: one worker per CPU

[tool.ruff]
line-length = 120
extend-exclude = [".rpgsync", "*.conflict-*.py"]

[tool.ruff.lint]
# The scripts use `from rpgsync.dsl import *` on purpose, and event/page
# functions are declarations that are never called.
# SIM102/SIM114/SIM117 must stay off: RPG Maker branches test one condition
# each, so nested ifs cannot be merged with and/or.
ignore = ["F403", "F405", "F811", "F841", "SIM102", "SIM114", "SIM117"]

[tool.ty.src]
exclude = [".rpgsync", "*.conflict-*.py"]

"""
    + PYRIGHT
)

GAME_TYPES = '''\
"""Types that depend on this game (written by rpgsync, do not edit).

Dynamic: what may stand, besides a number, where a command keeps a fixed number
(rpgsync.dsl.Fixed). {why}
"""

{body}
'''

TEST_SCRIPTS = '''\
"""Default checks for the event scripts.  Run with: uv run pytest (in parallel),
or from anywhere with: rpgsync check

Do not delete these tests: they are the same checks rpgsync runs before it
writes a script into the game.

- test_script_compiles: every script compiles into valid game data.
  rpgsync never writes a script that fails this.
- test_engine_compatibility: only commands the engine (and the patches
  declared in pyproject.toml [tool.rpgsync]: dynrpg, maniac, easyrpg)
  supports.  rpgsync never writes
  an event that fails this.
- test_map_references: teleports, vehicle locations, teleport and escape
  targets, and the start positions of a new game go to maps of the map
  tree, inside them.
- test_pages_reachable: warns about event pages that are never active (a
  page after them holds whenever they do: the active page is the last one
  whose conditions hold).  A warning, not a failure.
- test_message_width (commented out, an example to adapt): every message
  row fits in the message window.
- test_types: ty finds no wrong values (e.g. a misspelled direction or
  trigger).  Only run here: run it before you play-test.

Add your own tests in this folder.
"""

import shutil
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path

import pytest
from rpgsync.compat import check_specs, target_engine
from rpgsync.maptree import map_reference_problems, map_tree_maps
from rpgsync.project import Project
from rpgsync.script import unreachable_pages
from rpgsync.sync import ScriptError, Syncer

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["rpgsync"]
GAME = (ROOT / CONFIG["game"]).resolve()

syncer = Syncer(Project(str(GAME), str(ROOT)), log=lambda *_: None)
UNITS = [u for u in syncer.units() if u.has_script()]
PLACE_UNITS = [u for u in UNITS if u.kind in ("map", "common", "troop", "maptree")]  # scripts that name places
MAP_UNITS = [u for u in UNITS if u.kind == "map"]
MAPS, MAP_SIZES = map_tree_maps(syncer.project)  # the maps of RPG_RT.lmt, their sizes


def compile_unit(unit):
    try:
        return unit.compile_checked(unit.read_script().text)[0]  # a script split in files: joined
    except ScriptError as e:
        pytest.fail(str(e), pytrace=False)


@pytest.mark.parametrize("unit", UNITS, ids=[u.name for u in UNITS])
def test_script_compiles(unit):
    compile_unit(unit)


@pytest.mark.parametrize("unit", UNITS, ids=[u.name for u in UNITS])
def test_engine_compatibility(unit):
    engine = target_engine(str(ROOT), syncer.project.context().engine)
    problems = check_specs(compile_unit(unit), engine, CONFIG.get("patches", []), CONFIG.get("allow_commands", []))
    assert not problems, "\\n".join(problems)


@pytest.mark.parametrize("unit", PLACE_UNITS, ids=[u.name for u in PLACE_UNITS])
@pytest.mark.skipif(MAPS is None, reason="no map tree")
def test_map_references(unit):
    problems = map_reference_problems(compile_unit(unit), MAPS or {}, MAP_SIZES)
    assert not problems, "\\n".join("%s: %s" % (unit.at(line), problem) for line, problem in problems)


@pytest.mark.parametrize("unit", MAP_UNITS, ids=[u.name for u in MAP_UNITS])
def test_pages_reachable(unit):
    for line, problem in unreachable_pages(compile_unit(unit)):
        warnings.warn("%s: %s" % (unit.at(line), problem), stacklevel=1)


# Example of a check of your own: uncomment it to make sure every message
# fits in the message window, 50 characters per row (38 with a face) and
# 4 rows. \\n[3] counts as the hero's name, \\v[12] as 3 digits.
#
# @pytest.mark.parametrize("unit", UNITS, ids=[u.name for u in UNITS])
# def test_message_width(unit):
#     from rpgsync.messages import MessageLimits, message_problems
#
#     limits = MessageLimits(width=50, width_with_face=38, rows=4, variable_width=3)
#     problems = message_problems(compile_unit(unit), syncer.project.context(), limits)
#     assert not problems, "\\n".join(problems)


@pytest.mark.skipif(shutil.which("ty") is None, reason="ty is not installed")
def test_types():
    # the environment running the tests (the scripts' .venv), even when another virtualenv is active
    cmd = ["ty", "check", "--quiet", "--python", sys.prefix]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
'''

TESTS_README = """\
# Tests

`test_scripts.py` holds the default checks.  Run them with:

    uv run pytest

**Do not delete them.**  rpgsync runs the first two before it writes a
script into the game: a script that does not compile, or an event that uses
a command the engine does not support, is reported and the game file is left
untouched until you fix it.

| Test | What it checks | Also gates the sync |
| --- | --- | --- |
| `test_script_compiles` | every script compiles into valid game data | yes |
| `test_engine_compatibility` | no RPG Maker 2003-only command in a 2000 game, no patch command (DynRPG, Maniac, EasyRPG) unless declared, no DynParams comment that breaks the game | yes |
| `test_map_references` | teleports, vehicle locations, teleport and escape targets and new game start positions go to maps of the map tree, inside them | no |
| `test_pages_reachable` | warns about event pages that are never active (a page after them holds whenever they do) | no (a warning) |
| `test_message_width` | example, commented out: message rows fit the window (50 characters, 38 with a face, 4 rows) | no |
| `test_types` | ty finds no wrong value (misspelled direction, trigger...) | no: run `uv run pytest` or `rpgsync check` |

Set the target engine and allow patch commands in `../pyproject.toml`:

    [tool.rpgsync]
    engine = "2003"            # "2000" | "2003"
    patches = ["dynrpg"]       # dynrpg | maniac | easyrpg
    allow_commands = [3007]    # single command codes

Add your own tests next to `test_scripts.py`.
"""

VSCODE_SETTINGS = """\
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "files.exclude": {".rpgsync": true},
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true
  }
}
"""

VSCODE_EXTENSIONS = """\
{
  "recommendations": ["charliermarsh.ruff", "astral-sh.ty"]
}
"""

GITIGNORE = """\
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
# rpgsync bookkeeping (sync state, merge bases, backups)
.rpgsync/
*.conflict-*.py
"""


def rpgsync_checkout() -> Path | None:
    """The folder of the running rpgsync when it is a clone, None when it was
    installed from git."""
    if (RPGSYNC_ROOT / "pyproject.toml").exists():  # source checkout / editable install
        return RPGSYNC_ROOT
    try:  # uvx --from <clone> / uv tool install <clone>
        direct = json.loads(metadata.distribution("rpgsync").read_text("direct_url.json") or "{}")
        url = urlparse(direct.get("url", ""))
        path = Path(unquote(url.path))
        if url.scheme == "file" and (path / "pyproject.toml").exists():
            return path
    except (metadata.PackageNotFoundError, ValueError):
        pass
    return None


def rpgsync_source() -> str:
    """The [tool.uv.sources] line saying where the scripts project gets rpgsync."""
    if rpgsync_checkout():
        return LINK_SOURCE
    return 'rpgsync = { git = "%s" }' % RPGSYNC_GIT


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction (os.readlink reads both)."""
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            os.readlink(path)
            return True
        except (OSError, ValueError):
            pass
    return False


def link_rpgsync(script_dir: str | Path) -> bool:
    """Point <scripts>/.rpgsync/rpgsync at this computer's rpgsync folder, made again
    when it is missing or points elsewhere.  -> whether it changed."""
    target = rpgsync_checkout()
    link = Path(script_dir) / RPGSYNC_LINK
    if target is None or _same(link, target):
        return False
    if _is_link(link):
        (os.rmdir if os.name == "nt" else os.unlink)(link)  # the link only, never what it points at
    elif link.exists():
        return False  # a real folder: left alone
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi  # no right to make symlinks: a junction needs none

        _winapi.CreateJunction(str(target), str(link))
    return True


def _uses_link(pyproject_text: str) -> bool:
    try:
        source = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {}).get("sources", {}).get("rpgsync", {})
    except tomllib.TOMLDecodeError:
        return False
    return isinstance(source, dict) and source.get("path") == RPGSYNC_LINK


def _absolute(path: str) -> bool:
    """Absolute on this computer or another one (a pyproject.toml written on Windows)."""
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def detect_patches(game_dir: str) -> list[str]:
    """Patches the game visibly uses: DynRPG keeps its plugins in DynPlugins/,
    and EasyRPG.ini turns patches on in its [Patch] section (DynRPG=1, Maniac=1, EasyRPG=1)."""
    found = []
    if os.path.isdir(os.path.join(game_dir, "DynPlugins")):
        found.append("dynrpg")
    ini = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        ini.read(os.path.join(game_dir, "EasyRPG.ini"), encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return found
    section = next((ini[s] for s in ini.sections() if s.lower() == "patch"), None)
    if section is not None:
        for key, patch in (("dynrpg", "dynrpg"), ("maniac", "maniac"), ("easyrpg", "easyrpg")):
            value = section.get(key, "0").strip().lower()  # configparser keys are case-insensitive
            if value not in ("0", "false", "no", "off", "") and patch not in found:
                found.append(patch)
    return found


def game_types(project: Project) -> str:
    """The content of database/game_types.py, which rpgsync.dsl reads when type checking."""
    if project.has_dynparams():
        why = "This game runs the DynParams plugin (DynPlugins/DynParams.dll):\na variable, read when the command runs."
        body = "from rpgsync.dsl import Variable\n\nDynamic = Variable"
    else:
        why = "Nothing: this game does not run the DynParams plugin."
        body = "from typing import Never\n\nDynamic = Never"
    return GAME_TYPES.format(why=why, body=body)


# lines of tests/test_scripts.py as an older rpgsync wrote them -> as written now (a
# script split in files is read through its unit)
TEST_SCRIPTS_UPDATES = [
    (
        "UNITS = [u for u in syncer.units() if Path(u.py_path).exists()]",
        "UNITS = [u for u in syncer.units() if u.has_script()]",
    ),
    (
        '        return unit.compile_checked(Path(unit.py_path).read_text(encoding="utf-8"))[0]',
        "        return unit.compile_checked(unit.read_script().text)[0]  # a script split in files: joined",
    ),
    (
        '    assert not problems, "\\n".join("line %s: %s" % (line, problem) for line, problem in problems)',
        '    assert not problems, "\\n".join("%s: %s" % (unit.at(line), problem) for line, problem in problems)',
    ),
    (
        '        warnings.warn("%s:%s: %s" % (Path(unit.py_path).name, line, problem), stacklevel=1)',
        '        warnings.warn("%s: %s" % (unit.at(line), problem), stacklevel=1)',
    ),
]


def update_test_scripts(root: Path) -> bool:
    """Bring the lines of tests/test_scripts.py an older rpgsync wrote up to date (the
    ones found as they were written).  -> written"""
    path = root / "tests" / "test_scripts.py"
    try:
        text = original = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for old, new in TEST_SCRIPTS_UPDATES:
        text = text.replace(old, new)
    if text == original:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update_scripts_project(project: Project) -> list[str]:
    """Bring the scripts folder of a set-up game up to date with this rpgsync: the
    rpgsync source and Pylance settings of pyproject.toml, the .rpgsync/rpgsync link,
    database/game_types.py, tests/test_scripts.py.  Returns the written paths."""
    root = Path(project.script_dir)
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []  # not set up: reading a game never creates its scripts folder
    written = []
    text = original = pyproject.read_text(encoding="utf-8")
    old_source = _PATH_SOURCE.search(text)
    if old_source and _absolute(old_source["path"]) and rpgsync_checkout():
        text = text[: old_source.start()] + LINK_SOURCE + text[old_source.end() :]
    try:
        configured = "pyright" in tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError:
        configured = True  # a file being edited: left as it is
    if not configured:
        text = text.rstrip("\n") + "\n\n" + PYRIGHT
    if text != original:
        pyproject.write_text(text, encoding="utf-8")
        written.append(str(pyproject))
    if _uses_link(text):
        link_rpgsync(root)
    path = root / "database" / "game_types.py"
    content = game_types(project)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    if update_test_scripts(root):
        written.append(str(root / "tests" / "test_scripts.py"))
    return written


def init_scripts_project(project: Project) -> list[str]:
    """Create pyproject.toml & co in the scripts folder (existing files are
    kept).  Returns the written paths."""
    root = Path(project.script_dir)
    root.mkdir(parents=True, exist_ok=True)
    title = os.path.basename(project.game_dir)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "game"
    files = {
        "pyproject.toml": PYPROJECT.format(
            name=slug + "-scripts",
            title=title,
            source=rpgsync_source(),
            game=os.path.relpath(project.game_dir, root),
            engine={"2k": "2000", "2k3": "2003"}[project.context().engine],
            patches=json.dumps(detect_patches(project.game_dir)),
        ),
        "tests/test_scripts.py": TEST_SCRIPTS,
        "tests/README.md": TESTS_README,
        ".vscode/settings.json": VSCODE_SETTINGS,
        ".vscode/extensions.json": VSCODE_EXTENSIONS,
        ".gitignore": GITIGNORE,
        "database/game_types.py": game_types(project),
    }
    written = []
    for rel_path, content in files.items():
        path = root / rel_path
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    if _uses_link((root / "pyproject.toml").read_text(encoding="utf-8")):
        link_rpgsync(root)
    return written
