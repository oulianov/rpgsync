"""The scripts project set up on the first sync, and the type stubs it checks scripts against."""

import inspect

from rpgsync import dsl, named
from rpgsync.scaffold import detect_patches


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
