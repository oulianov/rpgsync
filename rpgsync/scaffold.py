"""Turn a script folder into a small uv project (IDE support, tests, linting)."""

from __future__ import annotations

import json
import os
import re
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from .project import Project

RPGSYNC_ROOT = Path(__file__).resolve().parent.parent
RPGSYNC_GIT = "https://github.com/oulianov/rpgsync"

PYPROJECT = """\
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
rpgsync = {source}

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
from pathlib import Path

import pytest
from rpgsync.compat import check_specs, target_engine
from rpgsync.project import Project
from rpgsync.sync import ScriptError, Syncer

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["rpgsync"]
GAME = (ROOT / CONFIG["game"]).resolve()

syncer = Syncer(Project(str(GAME), str(ROOT)), log=lambda *_: None)
UNITS = [u for u in syncer.units() if Path(u.py_path).exists()]


def compile_unit(unit):
    try:
        return unit.compile_checked(Path(unit.py_path).read_text(encoding="utf-8"))[0]
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
| `test_engine_compatibility` | no RPG Maker 2003-only command in a 2000 game, no patch command (DynRPG, Maniac, EasyRPG) unless declared | yes |
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


def rpgsync_source() -> str:
    """How the scripts project depends on rpgsync (TOML inline table)."""
    if (RPGSYNC_ROOT / "pyproject.toml").exists():  # source checkout / editable install
        return '{ path = "%s", editable = true }' % RPGSYNC_ROOT.as_posix()
    try:  # uvx --from <clone> / uv tool install <clone>
        direct = json.loads(metadata.distribution("rpgsync").read_text("direct_url.json") or "{}")
        url = urlparse(direct.get("url", ""))
        path = Path(unquote(url.path))
        if url.scheme == "file" and (path / "pyproject.toml").exists():
            return '{ path = "%s", editable = true }' % path.as_posix()
    except (metadata.PackageNotFoundError, ValueError):
        pass
    return '{ git = "%s" }' % RPGSYNC_GIT


def detect_patches(game_dir: str) -> list[str]:
    """Patches the game visibly uses: DynRPG keeps its plugins in DynPlugins/."""
    found = []
    if os.path.isdir(os.path.join(game_dir, "DynPlugins")):
        found.append("dynrpg")
    return found


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
    }
    written = []
    for rel_path, content in files.items():
        path = root / rel_path
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    return written
