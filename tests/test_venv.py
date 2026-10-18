"""The scripts' environment of a game folder copied or renamed: its commands
written again for where it is now."""

import os
import stat

from rpgsync.venv import repair


def _venv(tmp_path, folder, launcher_python):
    """A scripts folder with an environment made at another place."""
    script_dir = tmp_path / folder
    bin_dir = script_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_bytes(b"\xcf\xfa\xed\xfe\0binary")
    body = b"# -*- coding: utf-8 -*-\nimport sys\nfrom rpgsync.cli import main\nmain()\n"
    for name in ("rpgsync", "pytest"):
        path = bin_dir / name
        path.write_bytes(launcher_python + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    (bin_dir / "activate").write_text("VIRTUAL_ENV='%s/.venv'\nexport VIRTUAL_ENV\n" % (tmp_path / "old game"))
    return script_dir, bin_dir, body


def test_launchers_of_a_renamed_folder_point_at_it(tmp_path):
    old = tmp_path / "old game" / ".venv" / "bin" / "python"
    header = b"#!/bin/sh\n'''exec' '" + os.fsencode(old) + b"' \"$0\" \"$@\"\n' '''\n"
    script_dir, bin_dir, body = _venv(tmp_path, "new game", header)

    assert repair(str(script_dir)) == str(tmp_path / "old game" / ".venv")

    new = str(bin_dir / "python")
    launcher = (bin_dir / "rpgsync").read_bytes()
    assert launcher == b"#!/bin/sh\n'''exec' '" + os.fsencode(new) + b"' \"$0\" \"$@\"\n' '''\n" + body
    assert os.access(bin_dir / "rpgsync", os.X_OK)
    assert (bin_dir / "activate").read_text().startswith("VIRTUAL_ENV='%s'" % (script_dir / ".venv"))
    assert (bin_dir / "python").read_bytes() == b"\xcf\xfa\xed\xfe\0binary"
    # nothing more to do
    assert repair(str(script_dir)) is None


def test_the_editable_rpgsync_follows_the_folder(tmp_path):
    old = tmp_path / "old game" / ".venv" / "bin" / "python"
    header = b"#!/bin/sh\n'''exec' '" + os.fsencode(old) + b"' \"$0\" \"$@\"\n' '''\n"
    script_dir, bin_dir, body = _venv(tmp_path, "new game", header)
    site = script_dir / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "_rpgsync.pth").write_text("%s/.rpgsync/rpgsync/\n" % (tmp_path / "old game"))
    (site / "other.pth").write_text("/elsewhere/lib\n")

    repair(str(script_dir))

    assert (site / "_rpgsync.pth").read_text() == "%s/.rpgsync/rpgsync/\n" % script_dir
    assert (site / "other.pth").read_text() == "/elsewhere/lib\n"


def test_a_short_path_gets_the_shell_line_for_spaces(tmp_path):
    script_dir, bin_dir, body = _venv(tmp_path, "with space", b"#!/old/.venv/bin/python\n")

    assert repair(str(script_dir)) == "/old/.venv"
    assert (bin_dir / "pytest").read_bytes().startswith(b"#!/bin/sh\n'''exec' '")


def test_an_environment_where_it_was_made_is_left_alone(tmp_path):
    script_dir = tmp_path / "game"
    python = script_dir / ".venv" / "bin" / "python"
    script_dir, bin_dir, body = _venv(tmp_path, "game", b"#!" + os.fsencode(python) + b"\n")
    before = (bin_dir / "rpgsync").read_bytes()

    assert repair(str(script_dir)) is None
    assert (bin_dir / "rpgsync").read_bytes() == before


def test_no_environment(tmp_path):
    assert repair(str(tmp_path)) is None
