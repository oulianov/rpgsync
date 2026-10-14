"""Map events as classes: settings on the class, page_N pages, docstrings, page calls."""

import re

import pytest
from typer.testing import CliRunner

from rpgsync import cli
from rpgsync import script as S
from rpgsync.commands import CompileError, Ctx
from rpgsync.lcf import ArrayItem, Command, LcfFile, Struct
from rpgsync.project import Project
from rpgsync.sync import Syncer

CTX = Ctx(encoding="cp1252", engine="2k3")

GARY = '''@event(30, "Gary", x=17, y=16)
class Gary(Event):
    sprite = ("Actor1", 2)
    layer = "same"

    def page_1(self):
        """Waiting, day 1"""

    @page(when=switches[1])
    def page_2(self):
        text("Vous vous trompez !")
        Gary.page_1()

    @page(when=switches[2], sprite=None, layer="below")
    def page_3(self):
        pass
'''


def _compile(src):
    return S.compile_map_source("from rpgsync.dsl import *\n\n\n" + src, CTX)


def _setup(game):
    syncer = Syncer(Project(str(game)), log=lambda *_: None)
    unit = next(u for u in syncer.units() if u.kind == "map" and u.map_id == 1)
    return syncer, unit


def _text(path):
    return open(path, encoding="utf-8").read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _append(unit, src):
    _write(unit.py_path, _text(unit.py_path) + "\n\n" + src)


def _event(path, name):
    return next(it for it in LcfFile.load(path).root.get("events") if it.struct.get("name") == name)


def _calls(path, eid):
    """Call Event commands of an event: [kind, event, page]."""
    ev = next(it for it in LcfFile.load(path).root.get("events") if it.id == eid)
    return [list(c.params) for pg in ev.struct.get("pages") for c in pg.struct.get("event_commands") if c.code == 12330]


def _cli(*args, ok=True):
    r = CliRunner().invoke(cli.app, [str(a) for a in args])
    assert (r.exit_code == 0) == ok, r.output
    return r.output


# -- compile ------------------------------------------------------------------------


def test_class_settings_apply_to_every_page():
    ev = _compile(GARY)[0]
    p1, p2, p3 = (pg.props for pg in ev.pages)
    assert ev.ident == "Gary"
    assert p1["graphic"] == (b"Actor1", 2) and p1["layer"] == 1 and p1["condition"] == {}
    assert p2["graphic"] == (b"Actor1", 2) and p2["layer"] == 1 and p2["condition"] == {"switch_a": 1}
    assert p3["graphic"] == (b"", 0) and p3["layer"] == 0  # sprite=None, layer="below"
    # a page's tile replaces the class's sprite
    ev = _compile(GARY.replace("sprite=None", "tile=96"))[0]
    assert ev.pages[2].props["graphic"] == (b"", 96)
    # settings after the pages apply too (class attributes)
    moved = GARY.replace('    layer = "same"\n', "") + '\n    layer = "same"\n'
    assert [pg.props for pg in _compile(moved)[0].pages] == [pg.props for pg in _compile(GARY)[0].pages]


def test_docstring_is_the_first_comment():
    ev = _compile(GARY)[0]
    assert [(c.code, c.string) for c in ev.pages[0].commands] == [(12410, b"Waiting, day 1")]
    multi = GARY.replace(
        '"""Waiting, day 1"""', '"""Waiting\n\n        on day 1\n        """\n        # a note\n        wait(1)'
    )
    cmds = _compile(multi)[0].pages[0].commands
    assert [(c.code, c.string) for c in cmds[:4]] == [
        (12410, b"Waiting"),
        (22410, b""),
        (22410, b"on day 1"),
        (12410, b"a note"),
    ]
    assert cmds[4].code == 11410


def test_page_calls():
    ev = _compile(GARY)[0]
    assert [list(c.params) for c in ev.pages[1].commands if c.code == 12330] == [[1, 30, 1]]
    # a page function of any name is called by its position
    renamed = GARY.replace("def page_1(self):", "def waiting(self):").replace("Gary.page_1()", "Gary.waiting()")
    assert [list(c.params) for c in _compile(renamed)[0].pages[1].commands if c.code == 12330] == [[1, 30, 1]]


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('    layer = "same"', "    when = switches[3]", "when is not a setting of the event's pages (a page's own"),
        ('    layer = "same"', "    route = route(move_up)", "route is not a setting of the event's pages"),
        (
            '    layer = "same"',
            '    direction = "left"',
            "direction is not a setting of the event's pages (a page's own",
        ),
        ('    layer = "same"', '    color = "red"', "color is not a setting of the event's pages (the settings are"),
        ('    layer = "same"', '    layer = "same"\n    layer = "below"', "layer is set twice"),
        ('    layer = "same"', "    tile = 3", "sprite or tile, not both"),
        ('    layer = "same"', '    layer = "middle"', "layer must be one of"),
        ("class Gary(Event):\n", 'class Gary(Event):\n    """Gary"""\n', "an event has no description in RPG Maker"),
        ("class Gary(Event):", "class gary:", "an event is a class of Event"),
        ("def page_1(self):", "def page_1(x):", "a page takes self only: def page_1(self):"),
        ("def page_3(self):", "def page_1(self):", "page_1 is defined twice"),
        ("def page_3(self):", "def move(self):", "move is what every event has"),
        ("        Gary.page_1()", '        Gary.page_1()\n        "stray"', "a string alone does nothing"),
        ("Gary.page_1()", "Gary.page_9()", "Gary has no page_9 (its pages: page_1, page_2, page_3)"),
        ("Gary.page_1()", "this.page_1()", "this is whichever event runs the code: this.call(1)"),
        ("    @page(when=switches[1])", "    @page(when=switches[1])\n    @page()", "a page's only decorator is @page"),
    ],
)
def test_compile_errors(old, new, message):
    assert old in GARY
    with pytest.raises(CompileError, match=re.escape(message)):
        _compile(GARY.replace(old, new, 1))


def test_self_is_the_event():
    src = GARY.replace("        Gary.page_1()\n", "        self.page_1()\n        self.move(move_up)\n")
    cmds = _compile(src)[0].pages[1].commands
    assert [list(c.params) for c in cmds if c.code == 12330] == [[1, 30, 1]]
    assert [c.params[0] for c in cmds if c.code == 11330] == [30]
    with pytest.raises(CompileError, match="event Gary has no id yet"):
        _compile(src.replace('@event(30, "Gary", ', '@event(name="Gary", '))


def test_class_names():
    items = []
    for n, name in enumerate(["Haru Intro", "EV0002", "Guide", "Guide", "Event 2", "Route", "1er étage", "Gagné !"], 1):
        st = Struct("Event")
        st.set("name", name.encode("cp1252"))
        items.append(ArrayItem(id=n, struct=st))
    assert list(S.class_names(items, CTX).values()) == [
        "HaruIntro",
        "Event2",
        "Guide3",
        "Guide4",
        "Event2_5",
        "Event6",
        "Event7",
        "Gagne",
    ]


def test_function_form_still_compiles():
    old = (
        '@event(30, "Gary", x=17, y=16)\ndef gary():\n'
        '    @page(sprite=("Actor1", 2), layer="same")\n    def page_1():\n        text("Hi")\n'
    )
    ev = _compile(old)[0]
    assert ev.pages[0].props["graphic"] == (b"Actor1", 2) and ev.pages[0].commands[0].code == 10110


# -- decompile ----------------------------------------------------------------------


def test_class_settings_are_the_most_common():
    def page(graphic, layer=0):
        return {**S.PAGE_BASE, "condition": {}, "graphic": graphic, "layer": layer}

    a, b = (b"Actor1", 2), (b"Actor1", 3)
    base = S.class_settings([page(a, 1), page(b, 1), page(b, 0)])
    assert base["graphic"] == b and base["layer"] == 1
    assert S.class_settings([page(a), page(b)])["graphic"] == a  # tie: the earliest page's
    assert S.class_settings([]) == S.PAGE_BASE
    # a page states what differs from the class, sprite=None for no graphic
    assert S.page_kwargs_src(page((b"", 0), 1), CTX, base) == [("sprite", None)]
    assert S.page_kwargs_src(page((b"", 7), 1), CTX, base) == [("tile", 7)]
    assert S.page_kwargs_src(page(b, 1), CTX, base) == []


@pytest.mark.parametrize(
    "texts, doc",
    [
        (["Waiting, day 1"], ['"""Waiting, day 1"""']),
        (["Waiting", "", "on day 1"], ['"""Waiting', "", "on day 1", '"""']),
        (["trailing space "], None),
        ([" leading space"], None),
        (['ends with a quote"'], None),
        (["a \\ backslash"], None),
        (["", "starts empty"], None),
        (["ends empty", ""], None),
        (["@dynparams_add_param 1"], None),
    ],
)
def test_which_comments_become_docstrings(texts, doc):
    cmds = [Command(code=22410 if i else 12410, string=t.encode()) for i, t in enumerate(texts)]
    lines, n = S._leading_doc(cmds + [Command(code=10110, string=b"Hi")], CTX, "")
    assert lines == doc and n == (len(texts) if doc else 0)


def test_new_event_class_round_trip(exported2003):
    syncer, unit = _setup(exported2003)
    _append(unit, GARY.replace("@event(30, ", "@event(300, "))
    r = syncer.sync(unit)
    assert r.action == "import", r.message
    gary = _event(unit.bin_path, b"Gary")
    pages = [pg.struct for pg in gary.struct.get("pages")]
    assert [p.get("character_name") for p in pages] == [b"Actor1", b"Actor1", b""]
    assert [c.string for c in pages[0].get("event_commands") if c.code == 12410] == [b"Waiting, day 1"]
    assert _calls(unit.bin_path, gary.id) == [[1, gary.id, 1]]
    assert syncer.sync(unit).action == "none"
    # written back from the game, the event looks the same (switch names aside)
    syncer.sync(unit, "game")
    text = _text(unit.py_path)
    entry = text[text.index('@event(%d, "Gary"' % gary.id) :]
    assert entry.startswith(
        '@event(%d, "Gary", x=17, y=16)\nclass Gary(Event):\n    sprite = ("Actor1", 2)\n    layer = "same"\n\n'
        '    def page_1(self):\n        """Waiting, day 1"""\n\n    @page(when=switches' % gary.id
    )
    assert "        self.page_1()\n" in entry  # its own page
    assert re.search(
        r'    @page\(when=switches\S+, sprite=None, layer="below"\)\n    def page_3\(self\):\n        pass\n', entry
    )
    assert syncer.sync(unit).action == "none"


def test_exported_scripts_are_classes(exported2003):
    _, unit = _setup(exported2003)
    text = _text(unit.py_path)
    assert "# Each event is a class whose pages are page_1, page_2..." in text
    assert "class Event1(Event):" in text and not re.search(r"^def ", text, re.M)


# -- page names, calls ----------------------------------------------------------------

CALLER = """@event(name="Ulysse", x=5, y=9)
class Ulysse(Event):
    def page_1(self):
        Gary.page_2()
"""


def test_new_pages_are_named_by_their_number(exported2003):
    syncer, unit = _setup(exported2003)
    src = GARY.replace("@event(30, ", "@event(300, ").replace("def page_1(self):", "def waiting(self):")
    src = src.replace("Gary.page_1()", "Gary.waiting()").replace("def page_2(self):", "def accusation(self):")
    _append(unit, src + "\n\n" + CALLER.replace("Gary.page_2()", "Gary.accusation()"))
    r = syncer.sync(unit)
    assert (
        r.action == "import"
        and "pages named by their number: Gary.waiting -> page_1, Gary.accusation -> page_2" in r.message
    )
    text = _text(unit.py_path)
    assert "waiting" not in text and "accusation" not in text
    assert "        Gary.page_1()\n" in text and "        Gary.page_2()\n" in text
    gary, ulysse = _event(unit.bin_path, b"Gary"), _event(unit.bin_path, b"Ulysse")
    assert _calls(unit.bin_path, ulysse.id) == [[1, gary.id, 2]]
    assert syncer.sync(unit).action == "none"


def test_a_moved_page_takes_its_calls(exported2003):
    syncer, unit = _setup(exported2003)
    _append(unit, GARY.replace("@event(30, ", "@event(300, ") + "\n\n" + CALLER)
    assert syncer.sync(unit).action == "import"
    gary, ulysse = _event(unit.bin_path, b"Gary"), _event(unit.bin_path, b"Ulysse")
    assert _calls(unit.bin_path, ulysse.id) == [[1, gary.id, 2]]
    # page_2 moved after page_3
    text = _text(unit.py_path)
    a = text.index("    @page(when=switches", text.index("class Gary(Event):"))
    b = text.index("    @page(when=switches", a + 1)
    end = text.index("        pass\n", b) + len("        pass\n")
    text = text[:a] + text[b:end] + "\n" + text[a:b].rstrip("\n") + "\n" + text[end:]
    _write(unit.py_path, text)
    r = syncer.sync(unit)
    assert r.action == "import" and "Gary.page_3 -> page_2, Gary.page_2 -> page_3" in r.message, r.message
    assert _calls(unit.bin_path, ulysse.id) == [[1, gary.id, 3]]
    text = _text(unit.py_path)
    assert "        Gary.page_3()\n" in text
    assert text.index("def page_2(self):\n        pass") < text.index('def page_3(self):\n        text("Vous')
    assert syncer.sync(unit).action == "none"


def test_pages_moved_in_the_editor_warn_about_calls(exported2003):
    syncer, unit = _setup(exported2003)
    _append(unit, GARY.replace("@event(30, ", "@event(300, ") + "\n\n" + CALLER)
    assert syncer.sync(unit).action == "import"
    # the editor inserts a page before the others: Call Event still says page 2
    f = LcfFile.load(unit.bin_path)
    gary = next(it for it in f.root.get("events") if it.struct.get("name") == b"Gary")
    pages = list(gary.struct.get("pages"))
    gary.struct.set(
        "pages",
        [ArrayItem(id=1, struct=S.new_page_struct())]
        + [ArrayItem(id=n + 2, struct=p.struct) for n, p in enumerate(pages)],
    )
    f.save(unit.bin_path)
    r = syncer.sync(unit)
    assert r.action == "export", r.message
    assert re.search(r"Map0001\.py:\d+: Gary\.page_2\(\) keeps its page number, but Gary's pages changed", r.message)
    assert re.search(r"Map0001\.py:\d+: self\.page_1\(\) keeps its page number", r.message)  # in Gary itself
    # an edit inside a page renumbers nothing
    f = LcfFile.load(unit.bin_path)
    gary = next(it for it in f.root.get("events") if it.struct.get("name") == b"Gary")
    gary.struct.get("pages")[3].struct.set("layer", 2)
    f.save(unit.bin_path)
    r = syncer.sync(unit)
    assert r.action == "export" and "keeps its page number" not in r.message, r.message


# -- which page is active -----------------------------------------------------------


@pytest.mark.parametrize(
    "first, then, hidden",
    [
        ("when=switches[1]", "", True),  # no condition after it
        ("when=switches[1]", "when=switches[2]", False),
        ("when=switches[1] and switches[2]", "when=switches[2]", True),
        ("when=variables[1] == 5", "when=variables[1] >= 3", True),
        ("when=variables[1] >= 3", "when=variables[1] >= 5", False),
        ("when=variables[1] > 4", "when=variables[1] >= 5", True),
        ("when=variables[1] == 5", "when=variables[1] != 4", True),
        ("when=variables[1] != 4", "when=variables[1] != 4", True),
        ("when=variables[1] != 4", "when=variables[1] >= 0", False),
        ("when=variables[1] == 5", "when=variables[2] >= 3", False),
        ("when=timer1.seconds <= 10", "when=timer1.seconds <= 20", True),
        ("when=timer1.seconds <= 20", "when=timer1.seconds <= 10", False),
        ("when=items[3] in party", "when=items[3] in party", True),
        ("when=actors[1] in party", "when=actors[2] in party", False),
    ],
)
def test_unreachable_pages(first, then, hidden):
    src = (
        '@event(1, "A", x=0, y=0)\nclass A(Event):\n'
        "    @page(%s)\n    def page_1(self):\n        pass\n\n"
        "    @page(%s)\n    def page_2(self):\n        pass\n" % (first, then)
    )
    problems = S.unreachable_pages(_compile(src))
    if hidden:
        why = "no condition" if not then else "its conditions are among page_1's"
        assert problems == [(7, "A.page_1 is never active: page_2, after it, holds whenever page_1 does (%s)" % why)]
    else:
        assert problems == []


def test_gary_has_no_unreachable_page():
    assert S.unreachable_pages(_compile(GARY)) == []
    # page_1 is hidden by page_2, but page_2 runs it with Call Event
    assert S.unreachable_pages(_compile(GARY.replace("    @page(when=switches[1])\n", ""))) == []
    hidden = GARY.replace("    @page(when=switches[1])\n", "").replace("Gary.page_1()", "wait(1)")
    assert [p for _, p in S.unreachable_pages(_compile(hidden))] == [
        "Gary.page_1 is never active: page_2, after it, holds whenever page_1 does (no condition)"
    ]


def test_check_warns_about_unreachable_pages(exported2003):
    syncer, unit = _setup(exported2003)
    src = GARY.replace("@event(30, ", "@event(300, ").replace("    @page(when=switches[1])\n", "")
    _append(unit, src.replace("        Gary.page_1()\n", ""))
    assert syncer.sync(unit).action == "import"
    out = _cli("check", exported2003, "--no-types")
    assert re.search(
        r"Map0001\.py:\d+: Gary\.page_1 is never active: page_2, after it, holds whenever page_1 does", out
    )


# -- migration ------------------------------------------------------------------------


def test_function_events_are_rewritten_as_classes(exported2003):
    syncer, unit = _setup(exported2003)
    _append(
        unit,
        '@event(name="Guide", x=8, y=6)\ndef guide():\n'
        '    @page(sprite=("Actor1", 0))\n    def page_1():\n        text("Hello!")\n',
    )
    r = syncer.sync(unit)
    assert r.action == "import" and "events rewritten as classes" in r.message, r.message
    text = _text(unit.py_path)
    assert not re.search(r"^def ", text, re.M)
    assert re.search(
        r'@event\(\d+, "Guide", x=8, y=6\)\nclass Guide\w*\(Event\):\n    sprite = \("Actor1", 0\)\n', text
    )
    assert syncer.sync(unit).action == "none"


def _editor_edit(unit, name, fn):
    """The editor saves the map with a change to the event named `name`."""
    f = LcfFile.load(unit.bin_path)
    fn(next(it for it in f.root.get("events") if it.struct.get("name") == name).struct)
    f.save(unit.bin_path)


@pytest.mark.parametrize(
    "rename, cls",
    [
        (lambda t: t.replace('@event(300, "Gary"', '@event(300, "Gary Bis"'), "Gary"),  # the event, in the script
        (lambda t: re.sub(r"\bGary\b(?!\")", "Witness", t), "Witness"),  # the class, with an IDE
    ],
)
def test_class_names_belong_to_the_script(exported2003, rename, cls):
    """An event regenerated after an editor change keeps the class name of the script:
    the calls in the events left as they are still find it."""
    syncer, unit = _setup(exported2003)
    _append(unit, GARY.replace("@event(30, ", "@event(300, ") + "\n\n" + CALLER)
    assert syncer.sync(unit).action == "import"
    _write(unit.py_path, rename(_text(unit.py_path)))
    assert syncer.sync(unit).action == "import"
    name = b"Gary Bis" if "Gary Bis" in _text(unit.py_path) else b"Gary"
    _editor_edit(unit, name, lambda st: st.set("x", 18))  # only Gary is regenerated
    assert syncer.sync(unit).action == "export"
    text = _text(unit.py_path)
    assert re.search(r"@event\(300, .*\nclass %s\(Event\):" % cls, text)
    assert "        %s.page_2()" % cls in text  # Ulysse, left as it was
    assert syncer.sync(unit).action == "none"  # and it compiles


def test_editor_rename_renames_the_class(exported2003):
    syncer, unit = _setup(exported2003)
    _append(unit, GARY.replace("@event(30, ", "@event(300, ") + "\n\n" + CALLER)
    assert syncer.sync(unit).action == "import"
    _editor_edit(unit, b"Gary", lambda st: st.set("name", b"Gary Bis"))
    assert syncer.sync(unit).action == "export"
    text = _text(unit.py_path)
    assert "class GaryBis(Event):" in text and "GaryBis.page_2()" in text and "Gary.page" not in text
