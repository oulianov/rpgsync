"""Format generated scripts with ruff, so they look like hand-written Python
and editors that format on save do not fight the generator.

The scripts folder's own ruff settings (``[tool.ruff]`` of its
pyproject.toml) are used, the same ones as the editor's ruff extension.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from collections.abc import Callable

LINE_LENGTH = 120  # when the scripts folder has no ruff settings


def _ruff(folder: str | None = None) -> str | None:
    """The scripts folder's own ruff (the version its editor uses), else the
    one installed with rpgsync, else one on the PATH."""
    if folder:
        for rel in (".venv/bin/ruff", ".venv/Scripts/ruff.exe"):
            path = os.path.join(folder, rel)
            if os.path.isfile(path):
                return path
    try:
        from ruff.__main__ import find_ruff_bin

        return find_ruff_bin()
    except Exception:
        return shutil.which("ruff")


def _has_ruff_config(folder: str) -> bool:
    try:
        with open(os.path.join(folder, "pyproject.toml"), "rb") as f:
            return "ruff" in tomllib.load(f).get("tool", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False


def line_length(folder: str | None) -> int:
    """The line length the scripts of `folder` are formatted to."""
    if folder:
        try:
            with open(os.path.join(folder, "pyproject.toml"), "rb") as f:
                return int(tomllib.load(f).get("tool", {}).get("ruff", {}).get("line-length", LINE_LENGTH))
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            pass
    return LINE_LENGTH


def format_source(
    text: str, same: Callable[[str], bool], path: str | None = None, folder: str | None = None
) -> tuple[str, str | None]:
    """ruff-format `text` (the content of `path`, a file of the scripts
    `folder`).  -> (text, problem): the original text and the reason when ruff
    is missing, fails, or the formatted code would not compile to the same
    events (`same`)."""
    ruff = _ruff(folder)
    if ruff is None:
        return text, "ruff is not installed (run `uv sync` in the scripts folder)"
    if folder and _has_ruff_config(folder):
        cmd = [ruff, "format", "--stdin-filename", path or "script.py", "-"]
    else:
        cmd = [ruff, "format", "--isolated", "--line-length", str(LINE_LENGTH), "-"]
    try:
        out = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=60, cwd=folder or None)
    except (OSError, subprocess.TimeoutExpired) as e:
        return text, "ruff failed: %s" % e
    if out.returncode != 0:
        return text, "ruff failed: %s" % out.stderr.decode("utf-8", "replace").strip()[:300]
    formatted = out.stdout.decode("utf-8")
    if formatted == text:
        return text, None
    if not same(formatted):
        return text, "ruff's layout would change the game data (please report this)"
    return formatted, None
