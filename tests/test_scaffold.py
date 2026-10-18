"""The scripts project set up on the first sync, and the type stubs it checks scripts against."""

import ast
import inspect
import tomllib

from rpgsync import dsl, named
from rpgsync.project import Project
from rpgsync.scaffold import (
    LINK_SOURCE,
    PYPROJECT,
    RPGSYNC_ROOT,
    detect_patches,
    init_scripts_project,
    update_scripts_project,
)


def test_no_patches(tmp_path):
    assert detect_patches(str(tmp_path)) == []


def test_patches_from_dynplugins(tmp_path):
    (tmp_path / "DynPlugins").mkdir()
    assert detect_patches(str(tmp_path)) == ["dynrpg"]


def test_patches_from_easyrpg_ini(tmp_path):
    (tmp_path / "EasyRPG.ini").write_text(
        "[Game]\nEngine=rpg2k3e\n\n[Patch]\nManiac=1\nEasyRPG=1\nDynRPG=0\nKeyPatch=1\n"
    )
    assert detect_patches(str(tmp_path)) == ["maniac", "easyrpg"]


def test_patches_dynrpg_once(tmp_path):
    (tmp_path / "DynPlugins").mkdir()
    (tmp_path / "EasyRPG.ini").write_text("[patch]\ndynrpg=1\n")
    assert detect_patches(str(tmp_path)) == ["dynrpg"]


def test_unreadable_ini(tmp_path):
    (tmp_path / "EasyRPG.ini").write_bytes(b"\xff\xfe garbage [[[")
    assert detect_patches(str(tmp_path)) == []


def test_every_command_stub_takes_extra():
    """The decompiler writes leftover parameters as extra=(...): the type stubs must accept it."""
    owners = {None: dsl, "char": dsl.Character, "vehicle": dsl.VehicleCharacter, "actors": dsl.Actor}
    owners |= {"pictures": dsl.Picture, "enemies": dsl.Enemy}
    missing = [
        spec.name
        for spec in named.SPECS
        if spec.extra and "extra" not in inspect.signature(getattr(owners[spec.target], spec.name)).parameters
    ]
    assert missing == []


def test_star_import_names():
    """`from rpgsync.dsl import *` gives every public name of the DSL but CommonEvents
    (database/common_events.py defines its own) and the typing helpers."""
    tree = ast.parse(inspect.getsource(dsl))
    defined = set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.ClassDef)):
            defined.add(st.name)
        elif isinstance(st, ast.Assign):
            defined |= {t.id for t in st.targets if isinstance(t, ast.Name)}
    public = {n for n in defined if not n.startswith("_")} - {"CommonEvents", "Fixed"}
    assert sorted(public - set(dsl.__all__)) == []
    assert sorted(set(dsl.__all__) - public) == []
    assert dsl.__all__ == sorted(dsl.__all__)


def test_pyproject_template_is_toml():
    config = tomllib.loads(
        PYPROJECT.format(name="g", title="G", source=LINK_SOURCE, game="..", engine="2003", patches="[]")
    )
    assert config["tool"]["pyright"] == {"reportWildcardImportFromLibrary": False}
    assert config["tool"]["uv"]["sources"]["rpgsync"] == {"path": ".rpgsync/rpgsync", "editable": True}


def test_game_types(game2003):
    project = Project(str(game2003))
    scripts = game2003 / "Scripts"
    assert update_scripts_project(project) == []  # not set up: nothing written
    init_scripts_project(project)
    types = scripts / "database" / "game_types.py"
    assert "Dynamic = Never" in types.read_text()
    (game2003 / "DynPlugins").mkdir()
    (game2003 / "DynPlugins" / "DynParams.dll").write_bytes(b"")
    assert update_scripts_project(project) == [str(types)]
    assert "Dynamic = Variable" in types.read_text()
    assert update_scripts_project(project) == []


def test_pyright_settings_added_once(game2003):
    project = Project(str(game2003))
    init_scripts_project(project)
    pyproject = game2003 / "Scripts" / "pyproject.toml"
    old = pyproject.read_text().split("[tool.pyright]")[0].rstrip("\n") + "\n"  # as an older rpgsync wrote it
    pyproject.write_text(old)
    assert update_scripts_project(project) == [str(pyproject)]
    assert tomllib.loads(pyproject.read_text())["tool"]["pyright"] == {"reportWildcardImportFromLibrary": False}
    assert update_scripts_project(project) == []
    pyproject.write_text(old + "\n[tool.pyright]\nstrict = []\n")  # the user's own settings are kept
    assert update_scripts_project(project) == []


def test_rpgsync_found_through_a_link(game2003):
    """pyproject.toml (and uv.lock) name rpgsync by the same relative path on every
    computer: .rpgsync/rpgsync, a link to this computer's rpgsync folder."""
    project = Project(str(game2003))
    init_scripts_project(project)
    link = game2003 / "Scripts" / ".rpgsync" / "rpgsync"
    assert link.is_symlink() and link.resolve() == RPGSYNC_ROOT
    link.unlink()  # rpgsync moved: the link follows
    link.symlink_to(game2003, target_is_directory=True)
    assert update_scripts_project(project) == []
    assert link.resolve() == RPGSYNC_ROOT


def test_absolute_path_of_an_older_rpgsync(game2003):
    project = Project(str(game2003))
    init_scripts_project(project)
    pyproject = game2003 / "Scripts" / "pyproject.toml"
    text = pyproject.read_text()
    for path in ("/Users/someone/rpgsync", "C:/Users/someone/rpgsync"):
        pyproject.write_text(text.replace(LINK_SOURCE, 'rpgsync = { path = "%s", editable = true }' % path))
        assert update_scripts_project(project) == [str(pyproject)]
        assert pyproject.read_text() == text
    custom = text.replace(LINK_SOURCE, 'rpgsync = { path = "../rpgsync", editable = true }')
    pyproject.write_text(custom)  # a relative path of the user's own is kept
    assert update_scripts_project(project) == []
