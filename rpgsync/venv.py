"""The scripts' Python environment (their .venv) after the game folder moved.

uv writes the absolute path of the environment into its commands (the
launchers of rpgsync, pytest... in .venv/bin) and activate scripts. A game
folder copied or renamed keeps them pointing at the old folder: they fail, or
run the old folder's environment. Its Python itself still works (a link to
uv's), so the paths are written again for where the environment is now, as is
the old folder's .rpgsync/rpgsync named by the editable install of rpgsync.

On Windows the launchers are programs (.exe), not rewritten here:
``uv sync --reinstall`` in the scripts folder makes them again.
"""

from __future__ import annotations

import glob
import os
import re
import shlex

# the first lines of a launcher: "#!/path/.venv/bin/python", or for a path
# with spaces a shell line running it (#!/bin/sh, '''exec' '/path/python' "$0" "$@", ' ''')
_SHEBANG = re.compile(rb"\A#!(?P<python>\S+/bin/python[0-9.]*)[ \t]*\r?\n")
_SHELL = re.compile(rb"\A#!/bin/sh\r?\n'''exec' (?P<python>'(?:[^']|'\\'')*'|\S+) \"\$0\" \"\$@\"\r?\n' '''\r?\n")
_MAX_SIZE = 1 << 20  # launchers and activate scripts are small; the rest is left alone


def _unquote(word: bytes) -> str:
    return shlex.split(os.fsdecode(word))[0]


def _launcher_python(head: bytes) -> tuple[str, int] | None:
    """The Python a launcher runs, and the length of its header."""
    for pattern in (_SHELL, _SHEBANG):
        m = pattern.match(head)
        if m:
            return _unquote(m.group("python")), m.end()
    return None


def _header(python: str) -> bytes:
    """A launcher's first lines for this Python (as uv writes them)."""
    if " " not in python and len(python) < 127:
        return b"#!" + os.fsencode(python) + b"\n"
    return b"#!/bin/sh\n'''exec' " + os.fsencode(shlex.quote(python)) + b" \"$0\" \"$@\"\n' '''\n"


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def repair(script_dir: str) -> str | None:
    """Point the commands of the scripts' environment at where it is now.
    -> the folder of the environment they pointed at, None when nothing changed."""
    venv = os.path.join(os.path.abspath(script_dir), ".venv")
    bin_dir = os.path.join(venv, "bin")
    if not os.path.isdir(bin_dir):
        return None
    files = []
    old = None
    for name in sorted(os.listdir(bin_dir)):
        path = os.path.join(bin_dir, name)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > _MAX_SIZE:
            continue
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data:  # a program (python, ruff, ty)
            continue
        files.append((path, data))
        found = _launcher_python(data)
        if found and old is None:
            python = found[0]
            if os.path.basename(os.path.dirname(python)) == "bin":
                old = os.path.dirname(os.path.dirname(python))
    if old is None or _same(old, venv):
        return None
    new_python = os.path.join(bin_dir, "python")
    old_b, new_b = os.fsencode(old), os.fsencode(venv)
    for path, data in files:
        found = _launcher_python(data)
        if found and os.path.dirname(os.path.dirname(found[0])) == old:
            fixed = _header(new_python) + data[found[1] :]
        else:
            # the activate scripts: VIRTUAL_ENV='...'
            fixed = data.replace(old_b, new_b)
        if fixed != data:
            with open(path, "r+b") as f:  # in place: the file keeps its mode
                f.write(fixed)
                f.truncate()
    # the editable install of rpgsync (a .pth file) names the old folder's .rpgsync/rpgsync link
    old_link = os.fsencode(os.path.join(os.path.dirname(old), ".rpgsync") + os.sep)
    new_link = os.fsencode(os.path.join(os.path.dirname(venv), ".rpgsync") + os.sep)
    for pth in glob.glob(os.path.join(glob.escape(venv), "lib", "python*", "site-packages", "*.pth")):
        with open(pth, "rb") as f:
            data = f.read()
        if old_link in data:
            with open(pth, "wb") as f:
                f.write(data.replace(old_link, new_link))
    return old
