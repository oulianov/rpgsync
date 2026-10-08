import glob
import os

import pytest

TESTGAME = os.environ.get("TESTGAME", os.path.join(os.path.dirname(__file__), "..", "..", "TestGame"))
GAMES = sorted(glob.glob(os.path.join(TESTGAME, "TestGame-*")))


def pytest_generate_tests(metafunc):
    if "game" in metafunc.fixturenames:
        if not GAMES:
            pytest.skip("EasyRPG TestGame not found (set TESTGAME=/path/to/TestGame)")
        metafunc.parametrize("game", GAMES, ids=[os.path.basename(g) for g in GAMES])
    if "game_map" in metafunc.fixturenames:  # one test per map, so they run in parallel
        if not GAMES:
            pytest.skip("EasyRPG TestGame not found (set TESTGAME=/path/to/TestGame)")
        maps = [(g, m) for g in GAMES for m in sorted(glob.glob(os.path.join(g, "*.lmu")))]
        ids = ["%s/%s" % (os.path.basename(g), os.path.basename(m)[:-4]) for g, m in maps]
        metafunc.parametrize("game_map", maps, ids=ids)


@pytest.fixture
def game2003(tmp_path):
    """A writable copy of the RPG Maker 2003 test game."""
    import shutil

    src = os.path.join(TESTGAME, "TestGame-2003")
    if not os.path.isdir(src):
        pytest.skip("TestGame-2003 not found")
    dst = tmp_path / "game"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("Scripts"))
    return dst


@pytest.fixture(scope="session")
def _exported2003(tmp_path_factory):
    """The 2003 test game with its scripts exported, once per test worker."""
    import shutil

    from rpgsync.project import Project
    from rpgsync.sync import Syncer

    src = os.path.join(TESTGAME, "TestGame-2003")
    if not os.path.isdir(src):
        pytest.skip("TestGame-2003 not found")
    dst = tmp_path_factory.mktemp("exported") / "game"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("Scripts"))
    syncer = Syncer(Project(str(dst)), log=lambda *_: None)
    for unit in syncer.units():
        assert syncer.sync(unit).action == "export"
    return dst


@pytest.fixture
def exported2003(_exported2003, tmp_path):
    """A writable copy of the 2003 test game, scripts already exported and in sync."""
    import shutil

    dst = tmp_path / "game"
    shutil.copytree(_exported2003, dst)
    return dst
