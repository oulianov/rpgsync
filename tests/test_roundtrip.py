"""Lossless round trips over every map and database of the EasyRPG TestGame suites."""

import glob
import os

from rpgsync import script as S
from rpgsync.lcf import ArrayItem, LcfFile, Struct
from rpgsync.project import Project


def _touch_all(s: Struct):
    """Decode every schema chunk and mark it dirty to force re-encoding."""
    for c in list(s.chunks):
        if s._field(c.id)[2] == "raw":
            continue
        v = s.get(c.id)
        c.dirty = True
        if isinstance(v, Struct):
            _touch_all(v)
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, ArrayItem):
                    _touch_all(it.struct)


def test_binary_roundtrip(game):
    for path in sorted(glob.glob(os.path.join(game, "*.lmu")) + [os.path.join(game, "RPG_RT.ldb")]):
        data = open(path, "rb").read()
        f = LcfFile.parse(data)
        assert f.to_bytes() == data, path
        _touch_all(f.root)
        assert f.to_bytes() == data, "re-encoding changed " + path


def test_map_script_roundtrip(game_map, tmp_path):
    game, path = game_map
    ctx = Project(game, script_dir=str(tmp_path)).context()
    data = open(path, "rb").read()
    f = LcfFile.parse(data)
    text = S.decompile_map(f.root, ctx, os.path.basename(path)[:-4], os.path.basename(path))
    assert "raw=True" not in text, path
    specs = S.compile_map_source(text, ctx)
    assert [s.key() for s in specs] == [s.key() for s in S.map_event_specs(f.root, ctx)], path
    f2 = LcfFile.parse(data)
    summary = S.apply_events(f2.root, specs)
    assert not any(summary.values()), (path, summary)
    assert f2.to_bytes() == data, path


def test_common_events_script_roundtrip(game, tmp_path):
    proj = Project(game, script_dir=str(tmp_path))
    ctx = proj.context()
    data = open(proj.ldb_path, "rb").read()
    text = S.decompile_common_events(LcfFile.parse(data).root, ctx, "RPG_RT.ldb")
    db = LcfFile.parse(data)
    summary = S.apply_common_events(db.root, S.compile_common_events_source(text, ctx))
    assert not any(summary.values()), summary
    assert db.to_bytes() == data
