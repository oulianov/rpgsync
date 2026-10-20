"""The game's resource files (CharSet/, Picture/, Sound/...) and what refers to them:
files imported but never used, and references to files the game does not have.

The references come from the compiled scripts (events, the database tables, the map
tree), with their lines, and from the parts of the game files rpgsync does not write
as scripts: the system settings, battle animations and chipsets of RPG_RT.ldb, and
the panoramas of the maps.  A name is found as the engine finds it: without its
extension, in any case, in the game folder, then in the RTP when the game uses one.
"""

from __future__ import annotations

import configparser
import os
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from . import db
from .lcf import Chunk, LcfError, LcfFile, Reader, Struct, decode_value, read_array, read_move_commands, read_struct

IMAGES = (".png", ".bmp", ".xyz")
SOUNDS = (".wav", ".mp3", ".ogg", ".oga", ".opus", ".wma", ".flac")
FOLDERS: dict[str, tuple[str, ...]] = {
    "Backdrop": IMAGES,
    "Battle": IMAGES,
    "Battle2": IMAGES,
    "BattleCharSet": IMAGES,
    "BattleWeapon": IMAGES,
    "CharSet": IMAGES,
    "ChipSet": IMAGES,
    "FaceSet": IMAGES,
    "Frame": IMAGES,
    "GameOver": IMAGES,
    "Monster": IMAGES,
    "Panorama": IMAGES,
    "Picture": IMAGES,
    "System": IMAGES,
    "System2": IMAGES,
    "Title": IMAGES,
    "Music": (*SOUNDS, ".mid", ".midi", ".mod", ".xm", ".it", ".s3m"),
    "Sound": SOUNDS,
    "Movie": (".avi", ".mpg", ".mpeg", ".wmv", ".mp4"),
}
_EXTENSIONS = {ext for exts in FOLDERS.values() for ext in exts}

# command code -> (folder of its string, how the scripts write it)
COMMANDS = {
    10130: ("FaceSet", "face()"),
    10630: ("CharSet", "actors[...].set_sprite()"),
    10640: ("FaceSet", "actors[...].set_face()"),
    10650: ("CharSet", "set_sprite() of a vehicle"),
    10660: ("Music", "set_system_bgm()"),
    10670: ("Sound", "set_system_se()"),
    10680: ("System", "set_system_graphics()"),
    10710: ("Backdrop", "start_battle()"),
    11110: ("Picture", "pictures[...].show()"),
    11510: ("Music", "play_bgm()"),
    11550: ("Sound", "play_se()"),
    11560: ("Movie", "play_movie()"),
    11720: ("Panorama", "set_panorama()"),
    13210: ("Backdrop", "set_battle_background()"),
}
MOVES = {34: ("CharSet", "change_graphic()"), 35: ("Sound", "play_se() in a move route")}
# Maniac Patch: the name of these commands can be a string variable's, when the 4 bits at
# `shift` of parameter `mode` are not 0 -> (mode, shift, parameter of the variable)
STRING_VARIABLES = {
    10130: (3, 0, 4),
    10630: (3, 1, 4),
    10640: (2, 1, 3),
    10660: (5, 0, 6),
    10670: (4, 0, 5),
    10680: (2, 0, 3),
    11110: (17, 2, 30),
    11510: (4, 0, 5),
    11550: (3, 0, 4),
    11720: (6, 0, 7),
}
_COMMENTS = (12410, 22410)
_SEPARATORS = re.compile(r"[\s\"'`,;:()\[\]{}<>=|/\\*?!]+")  # around a name in a comment or setting
# the entries holding files in their own entries (the other fields are not walked: an
# item's battle animations per actor alone are tens of thousands of entries)
NESTED = {
    "Terrain": ("footstep",),
    "Skill": ("sound_effect",),
    "BattlerAnimation": ("poses", "weapons"),
    "MapInfo": ("music",),
}
_FILE_MODELS = {"Actor", "Enemy", "Terrain", "BattlerAnimationPose", "BattlerAnimationWeapon", "MapInfo", *NESTED}
LABELS = {
    "Actor": "actor",
    "Enemy": "enemy",
    "Terrain": "terrain",
    "Skill": "skill",
    "BattlerAnimation": "battler animation",
    "BattlerAnimationPose": "pose",
    "BattlerAnimationWeapon": "weapon",
    "MapInfo": "map",
}
_OFF = "(off)"  # a sound or music set to none


class Ref(BaseModel):
    """One reference to a resource file."""

    folder: str
    name: str  # as the game writes it
    path: str  # the script (or game file) that refers to it
    line: int | None = None
    what: str = ""  # what refers to it, for the message
    active: bool = True  # False: kept by the editor for a setting not chosen (not loaded)
    event: bool = False  # from an event's commands or page (else the database, the map tree...)


def key(name: str) -> str:
    """A resource name as the engine looks it up: any case, no extension, / between folders."""
    k = unicodedata.normalize("NFC", name.strip()).replace("\\", "/").lower().lstrip("/")
    k = k[2:] if k.startswith("./") else k
    root, ext = os.path.splitext(k)
    return root if ext in _EXTENSIONS else k


def files(root: str) -> dict[str, dict[str, str]]:
    """folder -> {key: file path in the folder} of the resource files under `root`."""
    out: dict[str, dict[str, str]] = {}
    try:
        entries = os.listdir(root)
    except OSError:
        return out
    for entry in entries:
        folder = next((f for f in FOLDERS if f.lower() == entry.lower()), None)
        top = os.path.join(root, entry)
        if folder is None or not os.path.isdir(top):
            continue
        found = out.setdefault(folder, {})
        for dirpath, _, names in os.walk(top):
            for name in names:
                if os.path.splitext(name)[1].lower() in FOLDERS[folder]:  # not Thumbs.db, .DS_Store...
                    rel = os.path.relpath(os.path.join(dirpath, name), top).replace(os.sep, "/")
                    found.setdefault(key(rel), rel)
    return out


def full_package(game_dir: str) -> bool:
    """Does the game ship every resource it uses (FullPackageFlag=1 in RPG_RT.ini)?"""
    ini = configparser.ConfigParser(strict=False, interpolation=None)
    name = next((n for n in os.listdir(game_dir) if n.lower() == "rpg_rt.ini"), None)
    if name is None:
        return False
    try:
        ini.read(os.path.join(game_dir, name), encoding="latin-1")
    except configparser.Error:
        return False
    section = next((ini[s] for s in ini.sections() if s.lower() == "rpg_rt"), None)
    return section is not None and section.get("fullpackageflag", "0").strip() == "1"


def rtp_dirs(engine: str) -> list[str]:
    """RTP folders installed as EasyRPG Player finds them: RPG2K_RTP_PATH (2000) or
    RPG2K3_RTP_PATH (2003), RPG_RTP_PATH, then rtp/2000 or rtp/2003 in the XDG data folders."""
    version = "2003" if engine == "2k3" else "2000"
    paths = []
    for var in ("RPG2K3_RTP_PATH" if engine == "2k3" else "RPG2K_RTP_PATH", "RPG_RTP_PATH"):
        paths += [p for p in re.split(r"[:;]", os.environ.get(var, "")) if p]
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share/:/usr/share/"
    paths += [os.path.join(d, "rtp", version) for d in [home, *data_dirs.split(":")] if d]
    return list(dict.fromkeys(p for p in paths if os.path.isdir(p)))


class Collector:
    """Gathers the references of a game, and the texts that may name files in ways
    the commands don't (plugin comments, DynParams building a name at runtime)."""

    def __init__(self, encoding: str):
        self.encoding = encoding
        self.refs: list[Ref] = []
        self._where: Callable[[int | None], tuple[str, int | None]] | None = None  # see specs()
        self.texts: list[str] = []  # comments: plugin commands may name files
        # (folder, prefix): names completed at runtime (a variable's number, DynParams) start
        # with these; folder None: any
        self.prefixes: set[tuple[str | None, str]] = set()

    def merge(self, other: Collector) -> None:
        """Add what another collector gathered (a script checked in another process)."""
        self.refs += other.refs
        self.texts += other.texts
        self.prefixes |= other.prefixes

    def _dec(self, raw: bytes | str) -> str:
        return raw if isinstance(raw, str) else bytes(raw).decode(self.encoding, "replace")

    def add(
        self, folder: str, name: bytes | str, path: str, line: int | None, what: str, active=True, event=False
    ) -> None:
        name = self._dec(name).strip()
        if self._where is not None:  # a script split in files: the file and line of the reference
            path, line = self._where(line)
        if name and name.lower() != _OFF:
            self.refs.append(Ref(folder=folder, name=name, path=path, line=line, what=what, active=active, event=event))

    # -- compiled scripts ------------------------------------------------------------
    def specs(
        self, specs: list, path: str, where: Callable[[int | None], tuple[str, int | None]] | None = None
    ) -> None:
        """The references of a script's specs (events, common events, troop events,
        database entries, map tree entries).  where: the file and line of a line of the
        script's text, when it is split in files."""
        self._where = where
        try:
            self._specs(specs, path)
        finally:
            self._where = None

    def _specs(self, specs: list, path: str) -> None:
        for spec in specs:
            if isinstance(spec, db.DbModel):
                self._model(spec, path, getattr(spec, "lineno", None), True)
                continue
            pages = getattr(spec, "pages", None)
            for page in pages if pages is not None else [spec]:
                line = getattr(page, "lineno", None) or getattr(spec, "lineno", None)
                props = getattr(page, "props", None) or {}
                if props.get("graphic"):
                    self.add("CharSet", props["graphic"][0], path, line, "the graphic of the page", event=True)
                route = props.get("route")
                for m in route[0] if route else []:
                    if m.code in MOVES:
                        self.add(MOVES[m.code][0], m.string, path, line, MOVES[m.code][1], event=True)
                self.commands(getattr(page, "commands", None) or [], path)

    def commands(self, cmds: list, path: str) -> None:
        from .dynparams import play

        rewritten = set()  # commands whose string DynParams replaces
        if any(c.code == 12410 and c.string[:11].lower() == b"@dynparams_" for c in cmds):
            rewritten = {o.target for o in play(cmds) if o.target is not None}
        for i, c in enumerate(cmds):
            if c.code in _COMMENTS:
                text = self._dec(c.string)
                self.texts.append(text.lower())
                # a name DynParams builds: its text, "quoted" or not (DynRPG takes both)
                m = re.match(r"\s*@dynparams_\w*append_string\s+(?:\"((?:[^\"]|\"\")+)\"|(.+))$", text, re.I)
                if m:
                    part = m.group(1).replace('""', '"') if m.group(1) is not None else m.group(2)
                    self.prefixes.add((None, key(part)))
                continue
            p = list(c.params)
            if c.code == 11330 and len(p) > 4:  # a move route: graphics and sounds of its steps
                try:
                    moves = read_move_commands(bytes(b & 0xFF for b in p[4:]))
                except (LcfError, IndexError, ValueError):
                    moves = []
                for m in moves:
                    if m.code in MOVES:
                        self.add(MOVES[m.code][0], m.string, path, c.line, MOVES[m.code][1], event=True)
                continue
            spec = COMMANDS.get(c.code)
            if spec is None:
                continue
            if c.code == 10710 and (len(p) < 3 or p[2] != 1):
                continue  # the battle background of the map or terrain, not an image
            var = STRING_VARIABLES.get(c.code)
            if var and len(p) > max(var[0], var[2]) and (p[var[0]] >> 4 * var[1]) & 0xF:
                continue  # Maniac Patch: the name is a string variable's, known when the game runs
            if c.code == 11110:
                # 2003 1.12 / Maniac Patch: the last digits replaced by a variable's value (PicPointer
                # patch: for picture ids from 50000, 4 digits)
                digits = p[18] if len(p) > 19 and p[19] else 4 if p and p[0] >= 50000 else 0
                name = self._dec(c.string)
                if digits:
                    if len(name) > digits:
                        self.prefixes.add(("Picture", key(name[:-digits])))
                    continue
            self.add(spec[0], c.string, path, c.line, spec[1], active=i not in rewritten, event=True)

    def _model(self, m: Any, path: str, line: int | None, active: bool) -> None:
        """A database / map tree entry and the entries it holds."""
        kind = type(m).__name__
        if kind not in _FILE_MODELS:
            return  # switches, variables, items, classes...: no file
        line = getattr(m, "lineno", None) or line
        label = LABELS.get(kind, kind.lower())
        if getattr(m, "id", None) is not None:
            label += " %s" % m.id
        if getattr(m, "name", None):
            label += " (%s)" % m.name
        if kind == "Actor":
            self.add("CharSet", m.character_name, path, line, label, active)
            self.add("FaceSet", m.face_name, path, line, label, active)
        elif kind == "Enemy":
            self.add("Monster", m.battler_name, path, line, label, active)
        elif kind == "Terrain":
            frame = m.background_type in ("frame", 1)
            self.add("Backdrop", m.background_name, path, line, label, active and not frame)
            self.add("Frame", m.background_a_name, path, line, label, active and frame)
            self.add("Frame", m.background_b_name, path, line, label, active and frame and bool(m.background_b))
        elif kind == "BattlerAnimationPose":
            self.add(
                "BattleCharSet", m.battler_name, path, line, label, active and m.animation_type in ("character", 0)
            )
        elif kind == "BattlerAnimationWeapon":
            self.add("BattleWeapon", m.weapon_name, path, line, label, active)
        elif kind == "MapInfo":
            self.add("Backdrop", m.background_name, path, line, label, active and m.background_type in ("specific", 2))
        for field in NESTED.get(kind, ()):
            value = getattr(m, field)
            on = active
            if kind == "MapInfo" and field == "music":
                on = active and m.music_type in ("specific", 2)
            elif kind == "Skill" and field == "sound_effect":
                on = active and m.type in ("teleport", "escape", "switch", 1, 2, 3)
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, (db.Sound, db.Music)):
                    self.add(type(v).__name__, v.name, path, line, "%s, %s" % (label, field.replace("_", " ")), on)
                elif isinstance(v, db.DbModel):
                    self._model(v, path, line, on)

    # -- game files ------------------------------------------------------------------
    def game(self, game_dir: str, ldb_path: str) -> None:
        """What only the game files hold: the system settings, battle animations and
        chipsets of the database, the panoramas of the maps."""
        try:
            root = LcfFile.load(ldb_path).root
        except (OSError, LcfError):
            root = None
        if root is not None:
            self._system(root._chunk(0x16), "RPG_RT.ldb")
            for it in _array(root._chunk(0x13), "Animation"):
                label = "battle animation %d (%s)" % (it.id, self._dec(_value(it.struct, 1, "str", b"")))
                large = bool(_value(it.struct, 3, "bool", False))
                self.add("Battle2" if large else "Battle", _value(it.struct, 2, "str", b""), "RPG_RT.ldb", None, label)
                for timing in _array(it.struct._chunk(6), "AnimationTiming"):
                    self._sound(timing.struct._chunk(2), "Sound", label + ", a sound")
            for it in _array(root._chunk(0x14), "Chipset"):
                label = "chipset %d (%s)" % (it.id, self._dec(_value(it.struct, 1, "str", b"")))
                self.add("ChipSet", _value(it.struct, 2, "str", b""), "RPG_RT.ldb", None, label)
        for name in sorted(os.listdir(game_dir)):
            if re.fullmatch(r"map\d{4}\.lmu", name, re.I):
                try:
                    m = LcfFile.load(os.path.join(game_dir, name)).root
                except (OSError, LcfError):
                    continue
                if m._chunk(0x20) is not None:
                    shown = bool(_value(m, 0x1F, "bool", False))
                    self.add("Panorama", _value(m, 0x20, "str", b""), name, None, "the panorama of the map", shown)
        for name in os.listdir(game_dir):  # plugin settings may name files (DynRPG.ini...)
            if name.lower().endswith(".ini"):
                try:
                    with open(os.path.join(game_dir, name), encoding=self.encoding, errors="replace") as f:
                        self.texts.append(f.read().lower())
                except OSError:
                    pass

    def _system(self, chunk: Chunk | None, path: str) -> None:
        if chunk is None:
            return
        st = read_struct(Reader(chunk.raw or b""), "System")
        for cid, folder, what in (
            (11, "CharSet", "the boat"),
            (12, "CharSet", "the ship"),
            (13, "CharSet", "the airship"),
            (17, "Title", "the title screen"),
            (18, "GameOver", "the game over screen"),
            (19, "System", "the system graphic"),
            (20, "System2", "the system 2 graphic"),
            (84, "Backdrop", "the battle test background"),
        ):
            self.add(folder, _value(st, cid, "str", b""), path, None, what)
        self.add(
            "Frame", _value(st, 100, "str", b""), path, None, "the screen frame", bool(_value(st, 99, "bool", False))
        )
        music = ("title", "battle", "battle end", "inn", "boat", "ship", "airship", "game over")
        for cid, what in zip(range(31, 39), music, strict=True):
            self._sound(st._chunk(cid), "Music", "the %s music" % what, path)
        sounds = ("cursor", "decision", "cancel", "buzzer", "battle start", "escape", "enemy attack", "enemy damaged")
        sounds += ("actor damaged", "dodge", "enemy death", "item")
        for cid, what in zip(range(41, 53), sounds, strict=True):
            self._sound(st._chunk(cid), "Sound", "the %s sound" % what, path)

    def _sound(self, chunk: Chunk | None, folder: str, what: str, path: str = "RPG_RT.ldb") -> None:
        if chunk is not None:
            self.add(folder, _value(read_struct(Reader(chunk.raw or b""), "Sound"), 1, "str", b""), path, None, what)


def _value(st: Struct, cid: int, kind: str, default: Any) -> Any:
    """A chunk of a structure rpgsync keeps raw (not in its schema)."""
    c = st._chunk(cid)
    return default if c is None or c.raw is None else decode_value(kind, c.raw)


def _array(chunk: Chunk | None, name: str) -> list:
    if chunk is None or chunk.raw is None:
        return []
    try:
        return read_array(Reader(chunk.raw), name)
    except (LcfError, IndexError):
        return []


class Problems(BaseModel):
    missing: list[Ref] = []  # references to files the game lacks
    unused: dict[str, list[str]] = {}  # folder -> files nothing refers to (paths in the folder)
    note: str | None = None  # why a part was not checked


def problems(collector: Collector, game_dir: str, engine: str, complete: bool) -> Problems:
    """The references to files that are not imported, and (with every script read:
    `complete`) the imported files nothing refers to."""
    out = Problems()
    have = files(game_dir)
    rtp: dict[str, set[str]] = {}
    check_missing = True
    if not full_package(game_dir):  # the game may use the RTP's files
        dirs = rtp_dirs(engine)
        for d in dirs:
            for folder, found in files(d).items():
                rtp.setdefault(folder, set()).update(found)
        if not dirs:
            check_missing = False
            out.note = (
                "the game uses the RTP (FullPackageFlag is not 1 in RPG_RT.ini) and no RTP was found: "
                "set %s to check the files it uses" % ("RPG2K3_RTP_PATH" if engine == "2k3" else "RPG2K_RTP_PATH")
            )
    if check_missing:
        for ref in collector.refs:
            k = key(ref.name)
            if ref.active and k not in have.get(ref.folder, {}) and k not in rtp.get(ref.folder, set()):
                if ref.folder in ("Music", "Sound") and ref.name.startswith("(") and ref.name.endswith(")"):
                    continue  # "(No Sound)": the Player plays nothing, as with (OFF)
                out.missing.append(ref)
    if complete:
        used = {(r.folder, key(r.name)) for r in collector.refs}
        texts = unicodedata.normalize("NFC", "\n".join(collector.texts))
        words = set()  # what the texts name: "hud", "hud.png" -> hud, hud.png
        for w in _SEPARATORS.split(texts):
            words.add(w)
            if "." in w:
                words.add(os.path.splitext(w)[0])
        for folder, found in sorted(have.items()):
            prefixes = [p for f, p in collector.prefixes if f in (None, folder) and len(p) > 1]
            unused = []
            for k, rel in sorted(found.items()):
                if (folder, k) in used or any(k.startswith(p) for p in prefixes):
                    continue
                name = os.path.basename(k)
                if name in words or (_SEPARATORS.search(name) and name in texts):
                    continue  # a plugin command or setting names it
                unused.append(rel)
            if unused:
                out.unused[folder] = unused
    return out
