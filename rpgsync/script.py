"""Event scripts: decompile LCF events to Python and compile them back.

Scripts are Python code that is parsed (never executed) and translated
statement by statement:

    if switches[3]:                       -> Conditional Branch / Else / Branch End
    while True: ... break                 -> Loop / Break Loop / End Loop
    match show_choices("Yes", "No"):      -> Show Choices / options / end
        case "Yes": ...
    variables[1] += 5                     -> Control Variables
    return                                -> End Event Processing

The decompiler only emits a form after checking that it compiles back to the
exact same commands; anything else falls back to the generic ``cmd(...)``.
"""

from __future__ import annotations

import ast
import bisect
import functools
import io
import keyword
import re
import tokenize
import unicodedata
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ConfigDict, SkipValidation

from . import commands as K
from . import dynparams as D
from . import pyexpr as P
from .commands import Call, Command, CompileError, Ctx, Raw, TableSize, render_call, size_stmt, split_size, table_length
from .lcf import ArrayItem, CommandList, IntVector, LcfFile, MoveCommand, Struct, decode_value, write_array

INDENT = "    "
evaluate = P.evaluate


# --------------------------------------------------------------------------
# Command list <-> tree
# --------------------------------------------------------------------------


class Node(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    cmd: SkipValidation[Command]
    body: list[Node] | None = None  # None for leaf commands


class StructureError(Exception):
    pass


def build_tree(cmds: list[Command]) -> list[Node]:
    pos = 0

    def parse(indent: int) -> list[Node]:
        nonlocal pos
        nodes = []
        while pos < len(cmds):
            c = cmds[pos]
            if c.indent < indent:
                break
            if c.indent > indent:
                raise StructureError("unexpected indent at command %d" % pos)
            pos += 1
            if pos < len(cmds) and cmds[pos].indent > indent:
                body = parse(indent + 1)
                if (
                    not body
                    or body[-1].body is not None
                    or body[-1].cmd.code != K.END
                    or body[-1].cmd.string
                    or body[-1].cmd.params
                ):
                    raise StructureError("block without END at command %d" % pos)
                nodes.append(Node(cmd=c, body=body[:-1]))
            else:
                nodes.append(Node(cmd=c))
        return nodes

    tree = parse(0)
    if pos != len(cmds):
        raise StructureError("negative indent")
    return tree


def flatten(nodes: list[Node]) -> list[Command]:
    out: list[Command] = []
    for n in nodes:
        out.append(n.cmd)
        if n.body is not None:
            out.extend(flatten(n.body))
            out.append(Command(code=K.END, indent=n.cmd.indent + 1))
    return out


def _keys(cmds: list[Command], base: int) -> list[tuple]:
    return [(c.code, c.indent - base, c.string, tuple(c.params)) for c in cmds]


# --------------------------------------------------------------------------
# Decompiling a command list
# --------------------------------------------------------------------------


# While true, each construct (a line, an if, a loop...) is rendered without compiling it
# back: body_lines checks the whole list once instead, and renders it again with the
# checks only when that fails.  Checking every construct costs one ast.parse per
# command, more for nested blocks (an if checks its whole body).
_TRUSTED: ContextVar[bool] = ContextVar("trusted_rendering", default=False)


def _compiles_to(lines: list[str], expected: list[Command], ctx: Ctx) -> bool:
    """Do these script lines compile to exactly `expected` (relative indents)?"""
    if not expected:
        return False
    if _TRUSTED.get():
        return True  # checked all at once by body_lines
    try:
        got = compile_body_src(lines, ctx)
    except (CompileError, SyntaxError, ValueError, TypeError, IndexError):
        return False
    return _keys(got, 0) == _keys(expected, expected[0].indent)


def stmt_src(c: Command, ctx: Ctx, in_loop: bool) -> str:
    """Best single-line form of a leaf command."""
    src = P.decode_stmt(c, ctx, in_loop)
    if src is not None and _compiles_to([src], [c], ctx):
        return src
    src = K.decode_command(c, ctx)
    if not src.startswith("cmd(") and not _compiles_to([src], [c], ctx):
        src = K.generic_src(c, ctx)
    return src


def body_lines(cmds: list[Command], ctx: Ctx, depth: int) -> tuple[list[str], bool]:
    """Render a command list.  Returns (lines, structured).

    If the structured rendering does not compile back to the identical list,
    a flat ``raw`` rendering with explicit indents is returned instead."""
    try:
        tree = build_tree(cmds)
    except StructureError:
        tree = None
    if tree is not None:
        # first without checking each construct, then (if the whole list does not
        # compile back identically) checking each one, which falls back where needed
        for trusted in (True, False):
            token = _TRUSTED.set(trusted)
            try:
                lines = _emit(tree, ctx, depth, False)
            finally:
                _TRUSTED.reset(token)
            try:
                if compile_body_src(lines, ctx) == cmds:
                    return lines, True
            except (CompileError, SyntaxError, ValueError, TypeError, IndexError):
                pass  # a trusted rendering that does not even compile
    lines = [INDENT * depth + K.generic_src(c, ctx, with_indent=True) for c in cmds]
    return lines, False


def _hint(c: Command, ctx: Ctx) -> str:
    h = K.hint(c, ctx) or _event_hint(c, ctx)
    return "  # " + h if h else ""


def _event_hint(c: Command, ctx: Ctx) -> str | None:
    """Name of the map event a command refers to (events[12] -> "Haru Intro")."""
    p = c.params
    ref = None
    if c.code in (11330, 10860) and p:
        ref = p[0]
    elif c.code == 11210 and len(p) > 1:
        ref = p[1]
    elif c.code == 12330 and len(p) > 1 and p[0] == 1:
        ref = p[1]
    elif c.code == 12010 and len(p) > 1 and p[0] == 6:
        ref = p[1]
    if ref is None or P.event_name(ref) is not None:  # already written by name
        return None
    name = ctx.event_names.get(ref)
    return "".join(ch for ch in name if ch.isprintable()) if name else None


def _body(lines: list[str], pad: str) -> list[str]:
    """A block body; Python needs at least one statement besides comments."""
    if any(l.strip() and not l.strip().startswith("#") for l in lines):
        return lines
    return lines + [pad + "pass"]


def _is_closer(n: Node, code: int) -> bool:
    return n.body is None and n.cmd.code == code and not n.cmd.params and not n.cmd.string


def _emit(nodes: list[Node], ctx: Ctx, level: int, in_loop: bool) -> list[str]:
    out: list[str] = []
    pad = INDENT * level
    i = 0
    prev_generic_block: int | None = None
    while i < len(nodes):
        n = nodes[i]
        c = n.cmd
        # closer of a generic `with cmd(...)` block group is implied
        if (
            prev_generic_block is not None
            and n.body is None
            and c.code in K.GROUPS
            and not c.string
            and not c.params
            and prev_generic_block in K.GROUPS[c.code][0]
        ):
            prev_generic_block = None
            i += 1
            continue
        prev_generic_block = None

        for construct in (_emit_text, _emit_if, _emit_while, _emit_match, _emit_handlers):
            r = construct(nodes, i, ctx, level, in_loop)
            if r is not None:
                lines, consumed = r
                if out and lines and out[-1].lstrip().startswith("#") and lines[0].lstrip().startswith("#"):
                    out.append("")  # separates two comment commands
                out.extend(lines)
                i += consumed
                break
        else:
            if n.body is None:
                out.append(pad + stmt_src(c, ctx, in_loop) + _hint(c, ctx))
            else:
                out.append(pad + "with " + K.generic_src(c, ctx) + ":" + _hint(c, ctx))
                out.extend(_body(_emit(n.body, ctx, level + 1, in_loop), pad + INDENT))
                prev_generic_block = c.code
            i += 1
    return out


def _emit_text(nodes, i, ctx, level, in_loop):
    n = nodes[i]
    c = n.cmd
    if n.body is not None or c.code not in K.TEXT_CODES or c.params:
        return None
    fname, cont = K.TEXT_CODES[c.code]
    lines = [ctx.dec(c.string)]
    j = i + 1
    while j < len(nodes) and nodes[j].body is None and nodes[j].cmd.code == cont and not nodes[j].cmd.params:
        lines.append(ctx.dec(nodes[j].cmd.string))
        j += 1
    split = not any("\n" in l for l in lines)  # else a line holds a line break: split=False
    pad = INDENT * level
    if fname == "comment":
        out = None
        if len(lines) == 1 and lines[0].startswith("@"):
            src = P.dynrpg_src(lines[0])
            if src is not None:
                out = [pad + src]
                hint = D.hints([m.cmd for m in nodes], ctx).get(i) if src.startswith("dyn.dynparams_") else None
                if hint is not None and _compiles_to(out, flatten(nodes[i:j]), ctx):
                    return [pad + src + "  # " + hint], j - i
        elif all(_hash_safe(l) for l in lines) and not lines[0].startswith("@"):
            out = [pad + ("# " + l if l else "#") for l in lines]
        if out is not None and _compiles_to(out, flatten(nodes[i:j]), ctx):
            return out, j - i
    if not split:
        return [pad + fname + "("] + [pad + INDENT + K.pystr(l) + "," for l in lines] + [
            pad + INDENT + "split=False,",
            pad + ")",
        ], j - i
    if len(lines) == 1:
        return [pad + render_call(fname, lines)], 1
    return [pad + fname + "("] + [pad + INDENT + K.pystr(l) + "," for l in lines] + [pad + ")"], j - i


def _hash_safe(line: str) -> bool:
    """Can this comment line be written as a '#' comment and read back?"""
    return line == line.rstrip() and all(ch.isprintable() and not 0xDC80 <= ord(ch) <= 0xDCFF for ch in line)


# branch command -> (else command, closer)
BRANCHES = {12010: (22010, 22011), 13310: (23310, 23311)}


def _emit_if(nodes, i, ctx, level, in_loop):
    n = nodes[i]
    if n.body is None or n.cmd.code not in BRANCHES or len(n.cmd.params) != 6:
        return None
    else_code, closer = BRANCHES[n.cmd.code]
    j = i + 1
    else_node = None
    if (
        j < len(nodes)
        and nodes[j].body is not None
        and nodes[j].cmd.code == else_code
        and not nodes[j].cmd.params
        and not nodes[j].cmd.string
    ):
        else_node = nodes[j]
        j += 1
    if not (j < len(nodes) and _is_closer(nodes[j], closer)):
        return None
    if n.cmd.params[5] != int(else_node is not None):
        return None
    if n.cmd.code == 13310:
        cond = None if n.cmd.string else P.battle_condition_src(n.cmd.params)
    else:
        cond = P.condition_src(n.cmd.params, ctx.dec(n.cmd.string))
    if cond is None:
        return None
    pad = INDENT * level
    lines = [pad + "if " + cond + ":" + _hint(n.cmd, ctx)]
    lines += _body(_emit(n.body, ctx, level + 1, in_loop), pad + INDENT)
    if else_node is not None:
        inner = _emit(else_node.body, ctx, level + 1, in_loop)
        if inner and inner[0].startswith(pad + INDENT + "if ") and _single_if(else_node.body, n.cmd.code):
            lines.append(pad + "el" + inner[0][len(pad + INDENT) :])
            lines += [l[len(INDENT) :] for l in inner[1:]]
        else:
            lines.append(pad + "else:")
            lines += _body(inner, pad + INDENT)
    consumed = j + 1 - i
    if not _compiles_to(lines, flatten(nodes[i : i + consumed]), ctx):
        return None
    return lines, consumed


def _single_if(body: list[Node], code: int = 12010) -> bool:
    """Is this else-body exactly one if/else/end construct of any kind (-> elif)?"""
    if not body or body[0].body is None or body[0].cmd.code not in BRANCHES:
        return False
    else_code, closer = BRANCHES[body[0].cmd.code]
    if len(body) == 2:
        return _is_closer(body[1], closer)
    if len(body) == 3:
        return body[1].body is not None and body[1].cmd.code == else_code and _is_closer(body[2], closer)
    return False


def _emit_while(nodes, i, ctx, level, in_loop):
    n = nodes[i]
    if n.body is None or n.cmd.code != 12210 or n.cmd.params or n.cmd.string:
        return None
    if not (i + 1 < len(nodes) and _is_closer(nodes[i + 1], 22210)):
        return None
    pad = INDENT * level
    lines = [pad + "while True:"] + _body(_emit(n.body, ctx, level + 1, True), pad + INDENT)
    if not _compiles_to(lines, flatten(nodes[i : i + 2]), ctx):
        return None
    return lines, 2


def _choice_sep(ctx: Ctx) -> str:
    """Separator of the show_choices summary (the line the editor shows).
    It depends on the editor version, so it is detected per game."""
    return ctx.choice_sep or ("/" if ctx.engine == "2k" else ", ")


def detect_choice_sep(map_paths, limit: int = 20) -> str | None:
    """Separator used by the show_choices commands of the game, if any."""
    votes = {"/": 0, ", ": 0}
    for path in map_paths:
        for ev in LcfFile.load(path).root.get("events"):
            for page in ev.struct.get("pages"):
                for c in page.struct.get("event_commands"):
                    if c.code == 10140:
                        found = [sep for sep in votes if sep.encode() in c.string]
                        if len(found) == 1:
                            votes[found[0]] += 1
                            if sum(votes.values()) >= limit:
                                return max(votes, key=votes.__getitem__)
    return max(votes, key=votes.__getitem__) if any(votes.values()) else None


# header command -> (case name per block command, closer)
MATCH_BLOCKS = {
    10710: ({20710: "victory", 20711: "escape", 20712: "defeat"}, 20713),
    10720: ({20720: "bought", 20721: "not_bought"}, 20722),
    10730: ({20730: "stayed", 20731: "declined"}, 20732),
}


def _emit_handlers(nodes, i, ctx, level, in_loop):
    """`match start_battle(...)` / `open_shop(...)` / `inn(...)` with their branches."""
    from . import named

    n = nodes[i]
    c = n.cmd
    if n.body is not None or c.code not in MATCH_BLOCKS:
        return None
    names, closer = MATCH_BLOCKS[c.code]
    j = i + 1
    cases = []
    while (
        j < len(nodes)
        and nodes[j].body is not None
        and nodes[j].cmd.code in names
        and not nodes[j].cmd.params
        and not nodes[j].cmd.string
    ):
        cases.append(nodes[j])
        j += 1
    if not cases or not (j < len(nodes) and _is_closer(nodes[j], closer)):
        return None
    ctx.match_header = True
    try:
        header = named.decode(c, ctx)
    finally:
        ctx.match_header = False
    if header is None:
        return None
    pad = INDENT * level
    lines = [pad + "match %s:" % header]
    for b in cases:
        lines.append(pad + INDENT + "case %s:" % K.pystr(names[b.cmd.code]))
        lines += _body(_emit(b.body, ctx, level + 2, in_loop), pad + INDENT * 2)
    consumed = j + 1 - i
    if not _compiles_to(lines, flatten(nodes[i : i + consumed]), ctx):
        return None
    return lines, consumed


def _emit_match(nodes, i, ctx, level, in_loop):
    n = nodes[i]
    c = n.cmd
    if n.body is not None or c.code != 10140 or len(c.params) != 1:
        return None
    j = i + 1
    blocks = []
    while j < len(nodes) and nodes[j].body is not None and nodes[j].cmd.code == 20140 and len(nodes[j].cmd.params) == 1:
        blocks.append(nodes[j])
        j += 1
    if not blocks or not (j < len(nodes) and _is_closer(nodes[j], 20141)):
        return None
    cancel = c.params[0]
    opts: list[str] = []
    cases = []
    for k, b in enumerate(blocks):
        idx = b.cmd.params[0]
        if idx == 4 and not b.cmd.string and k == len(blocks) - 1 and cancel == 5:
            cases.append(("_", b))
        elif idx == k:
            opts.append(ctx.dec(b.cmd.string))
            cases.append((K.pystr(opts[-1]), b))
        else:
            return None
    if (cancel == 5) != (cases[-1][0] == "_"):
        return None
    if len(set(opts)) != len(opts):  # repeated texts: cases by option index
        cases = [(p if p == "_" else str(k), b) for k, (p, b) in enumerate(cases)]
    kw = []
    if cancel not in (0, 5):
        kw.append("cancel=%d" % cancel)
    summary = ctx.dec(c.string)
    if summary != _choice_sep(ctx).join(opts):
        kw.append("summary=%s" % K.pystr(summary))
    pad = INDENT * level
    lines = [pad + "match show_choices(%s):" % ", ".join([K.pystr(o) for o in opts] + kw)]
    for pattern, b in cases:
        lines.append(pad + INDENT + "case %s:" % pattern)
        lines += _body(_emit(b.body, ctx, level + 2, in_loop), pad + INDENT * 2)
    consumed = j + 1 - i
    if not _compiles_to(lines, flatten(nodes[i : i + consumed]), ctx):
        return None
    return lines, consumed


# --------------------------------------------------------------------------
# Compiling statements
# --------------------------------------------------------------------------


class CommentIndex:
    """Full-line '#' comments of a script; they compile to Comment commands."""

    def __init__(self, src: str):
        self.lines = src.split("\n")
        self.items: list[list] = []  # [line, col, text, claimed]
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                line, col = tok.start
                if not self.lines[line - 1][:col].strip():
                    text = tok.string[1:]
                    self.items.append([line, col, text[1:] if text.startswith(" ") else text, False])
        self.item_lines = [item[0] for item in self.items]  # sorted: tokens come in order

    def region_end(self, after: int, col: int) -> int:
        """First code line after `after` that is indented less than `col`."""
        for n in range(after + 1, len(self.lines) + 1):
            l = self.lines[n - 1]
            if not l.strip() or l.lstrip().startswith("#"):
                continue
            if len(l) - len(l.lstrip()) < col:
                return n
        return len(self.lines) + 1

    def take(self, lo: int, hi: int, col: int, indent: int, ctx: Ctx) -> list[Command]:
        """Comment commands for unclaimed comments on lines lo < line < hi."""
        out: list[Command] = []
        last = None
        for i in range(bisect.bisect_right(self.item_lines, lo), len(self.items)):
            item = self.items[i]
            line, c, text, claimed = item
            if line >= hi:
                break
            if claimed or c < col:
                continue
            item[3] = True
            same_group = last is not None and line == last[0] + 1 and c == last[1]
            code = 22410 if same_group else 12410
            comment = Command(code=code, indent=indent, string=ctx.enc(text))
            comment.line = line
            out.append(comment)
            last = (line, c)
        return out


_COMPOUND = (ast.If, ast.While, ast.Match, ast.With)


def _with_call(stmt: ast.stmt) -> Call | None:
    """The cmd(...) of a generic `with cmd(...):` block."""
    if not isinstance(stmt, ast.With):
        return None
    if len(stmt.items) != 1 or stmt.items[0].optional_vars is not None:
        raise CompileError("use one command per with-statement", stmt)
    v = evaluate(stmt.items[0].context_expr)
    if not (isinstance(v, Call) and v.name == "cmd" and v.target is None):
        raise CompileError(
            "only generic `with cmd(...):` blocks exist; use if/while/match for branches, loops and choices", stmt
        )
    return v


def _with_code(stmt: ast.stmt | None) -> int | None:
    if stmt is None or not isinstance(stmt, ast.With):
        return None
    try:
        call = _with_call(stmt)
    except CompileError:
        return None
    h = call.args[0] if call.args else None
    return K.NAME_CODES.get(h) if isinstance(h, str) else h


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def compile_statements(
    stmts: list[ast.stmt], ctx: Ctx, indent: int, start: int | None = None, own_comments: bool = True
) -> list[Command]:
    """Compile a block body.  `start` is the line of the block header, used to
    attach the block's '#' comments.  The hidden else-body of an `elif` has no
    comments of its own (own_comments=False)."""
    ci: CommentIndex | None = ctx.comments if own_comments else None
    out: list[Command] = []
    col = stmts[0].col_offset if stmts else 0
    prev = start if start is not None else (stmts[0].lineno - 1 if stmts else 0)
    for i, stmt in enumerate(stmts):
        if ci is not None:
            out.extend(ci.take(prev, stmt.lineno, col, indent, ctx))
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
        try:
            cmds = _compile_stmt(stmt, nxt, ctx, indent)
        except CompileError as e:
            if e.lineno is None:
                e.lineno = stmt.lineno
            raise
        # the line of each command: the innermost statement that wrote it (the
        # inner blocks are compiled first), an int so the tree is not kept
        line = stmt.lineno
        for c in cmds:
            if c.line is None:
                c.line = line
        out.extend(cmds)
        prev = stmt.end_lineno
    if ci is not None:
        end = ci.region_end(stmts[-1].end_lineno, col) if stmts else len(ci.lines) + 1
        out.extend(ci.take(prev, end, col, indent, ctx))
    return out


def _block(
    body: list[ast.stmt], ctx: Ctx, indent: int, start: int | None = None, own_comments: bool = True
) -> list[Command]:
    return compile_statements(body, ctx, indent + 1, start, own_comments) + [Command(code=K.END, indent=indent + 1)]


def _is_elif(stmt: ast.If, ctx: Ctx) -> bool:
    """Is this else-branch written as `elif`?"""
    if len(stmt.orelse) != 1 or not isinstance(stmt.orelse[0], ast.If) or ctx.comments is None:
        return False
    return ctx.comments.lines[stmt.orelse[0].lineno - 1].lstrip().startswith("elif")


def _compile_stmt(stmt: ast.stmt, nxt: ast.stmt | None, ctx: Ctx, indent: int) -> list[Command]:
    def C(code, params=(), string=b""):
        return Command(code=code, indent=indent, params=list(params), string=string)

    if isinstance(stmt, ast.Pass) or _is_docstring(stmt):
        return []

    if isinstance(stmt, ast.If):
        test = evaluate(stmt.test)
        if isinstance(test, Call) and test.name == "battle_condition" and test.target is None:
            code, (else_code, closer) = 13310, BRANCHES[13310]
            params, string = P.battle_condition_val(test, ctx), b""
        else:
            code, (else_code, closer) = 12010, BRANCHES[12010]
            params, string = P.condition_val(test, ctx)
        out = [C(code, params + [int(bool(stmt.orelse))], string)] + _block(stmt.body, ctx, indent, stmt.lineno)
        if stmt.orelse:
            out += [C(else_code)] + _block(stmt.orelse, ctx, indent, stmt.body[-1].end_lineno, not _is_elif(stmt, ctx))
        return out + [C(closer)]

    if isinstance(stmt, ast.While):
        if not (isinstance(stmt.test, ast.Constant) and stmt.test.value is True) or stmt.orelse:
            raise CompileError("RPG Maker loops are `while True:` (leave them with break)", stmt)
        return [C(12210)] + _block(stmt.body, ctx, indent, stmt.lineno) + [C(22210)]

    if isinstance(stmt, ast.Match):
        return _compile_match(stmt, ctx, indent)

    if isinstance(stmt, ast.With):
        call = _with_call(stmt)
        cmd, explicit = K.generic_encode(call, ctx)
        cmd.indent = indent if explicit is None else explicit
        out = [cmd] + _block(stmt.body, ctx, indent, stmt.lineno)
        closer = K.CLOSER_OF.get(cmd.code)
        if closer is not None:
            nxt_code = _with_code(nxt)
            if nxt_code not in K.GROUPS[closer][1] and not _is_explicit(nxt, closer):
                out.append(C(closer))
        return out

    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id in ("text", "comment")
    ):
        call = evaluate(stmt.value)
        split = call.kwargs.get("split", True)
        if (
            set(call.kwargs) - {"split"}
            or not isinstance(split, bool)
            or not all(isinstance(a, str) for a in call.args)
        ):
            raise CompileError("%s() only takes strings, one per line (and split=False)" % call.name, stmt)
        lines: list[str] = []
        for a in call.args:
            # "\n" starts a new line, unless split=False: a line holding a line break
            lines.extend(a.split("\n") if split else [a])
        lines = lines or [""]
        first, cont = (10110, 20110) if call.name == "text" else (12410, 22410)
        return [C(first, string=ctx.enc(lines[0], stmt))] + [C(cont, string=ctx.enc(l, stmt)) for l in lines[1:]]

    cmd = P.encode_stmt(stmt, ctx)
    if cmd is not None:
        cmd.indent = indent
        return [cmd]
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = evaluate(stmt.value)
        cmd, explicit = K.encode_call(call, ctx)
        cmd.indent = indent if explicit is None else explicit
        return [cmd]
    raise CompileError("unsupported statement: %s" % ast.unparse(stmt).split("\n")[0], stmt)


def _is_explicit(stmt: ast.stmt | None, code: int) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    try:
        v = evaluate(stmt.value)
    except CompileError:
        return False
    if v.name != "cmd" or not v.args:
        return False
    h = v.args[0]
    return (K.NAME_CODES.get(h) if isinstance(h, str) else h) == code


def _compile_match(stmt: ast.Match, ctx: Ctx, indent: int) -> list[Command]:
    def C(code, params=(), string=b"", ind=indent):
        return Command(code=code, indent=ind, params=list(params), string=string)

    subject = evaluate(stmt.subject)
    if isinstance(subject, Call) and subject.target is None and subject.name in ("start_battle", "open_shop", "inn"):
        return _compile_handlers(stmt, subject, ctx, indent)
    if not (isinstance(subject, Call) and subject.name == "show_choices" and subject.target is None):
        raise CompileError('match is used for choices: match show_choices("Yes", "No"):', stmt)
    opts = subject.args
    if not opts or len(opts) > 4 or not all(isinstance(o, str) for o in opts):
        raise CompileError("show_choices() takes 1 to 4 option texts", stmt)
    a = K.Args(Call(name="show_choices", args=[], kwargs=subject.kwargs, node=subject.node), ["cancel", "summary"])
    wildcard = False
    texts: list[str] = []
    for k, case in enumerate(stmt.cases):
        pat = case.pattern
        if case.guard is not None:
            raise CompileError("case guards are not supported", case.pattern)
        if isinstance(pat, ast.MatchAs) and pat.pattern is None and pat.name is None:
            if k != len(stmt.cases) - 1:
                raise CompileError("case _: (the cancel branch) must come last", pat)
            wildcard = True
        elif (
            isinstance(pat, ast.MatchValue) and isinstance(pat.value, ast.Constant) and isinstance(pat.value.value, str)
        ):
            texts.append(pat.value.value)
        elif (
            isinstance(pat, ast.MatchValue)
            and isinstance(pat.value, ast.Constant)
            and type(pat.value.value) is int
            and pat.value.value == k
            and k < len(opts)
        ):
            texts.append(opts[k])  # case <option index>
        else:
            raise CompileError('cases are option texts, e.g. case "Yes":, or case _: for cancel', pat)
    if texts != list(opts):
        raise CompileError("the cases must list the options in order: %s" % ", ".join(map(repr, opts)), stmt)
    cancel = a.get("cancel")
    if wildcard:
        if cancel not in (None, "branch"):
            raise CompileError("with a `case _:` cancel branch, leave out cancel=", stmt)
        cv = 5
    elif cancel is None:
        cv = 0
    elif P.is_int(cancel) and 1 <= cancel <= 4:
        cv = cancel
    else:
        raise CompileError(
            "cancel= is the option number (1-4) chosen on cancel; use `case _:` for a separate cancel branch", stmt
        )
    summary = a.get("summary")
    if summary is None:
        summary = _choice_sep(ctx).join(opts)
    out = [C(10140, [cv], ctx.enc(summary, stmt))]
    for k, case in enumerate(stmt.cases):
        if k < len(opts):
            out.append(C(20140, [k], ctx.enc(opts[k], stmt)))
        else:
            out.append(C(20140, [4]))
        out += _block(case.body, ctx, indent, case.pattern.lineno)
    return out + [C(20141)]


def _compile_handlers(stmt: ast.Match, subject: Call, ctx: Ctx, indent: int) -> list[Command]:
    from . import named

    ctx.match_header = True
    try:
        header = named.encode(subject, ctx)
    finally:
        ctx.match_header = False
    header.indent = indent
    names, closer = MATCH_BLOCKS[header.code]
    codes = {v: k for k, v in names.items()}
    out = [header]
    for case in stmt.cases:
        pat = case.pattern
        if (
            not (isinstance(pat, ast.MatchValue) and isinstance(pat.value, ast.Constant) and pat.value.value in codes)
            or case.guard is not None
        ):
            raise CompileError("%s() cases are: %s" % (subject.name, ", ".join(repr(n) for n in codes)), pat)
        out.append(Command(code=codes[pat.value.value], indent=indent))
        out += _block(case.body, ctx, indent, case.pattern.lineno)
    return out + [Command(code=closer, indent=indent)]


def compile_raw_statements(stmts: list[ast.stmt], ctx: Ctx) -> list[Command]:
    out = []
    for stmt in stmts:
        if isinstance(stmt, ast.Pass) or _is_docstring(stmt):
            continue
        call = evaluate(stmt.value) if isinstance(stmt, ast.Expr) else None
        if not (isinstance(call, Call) and call.name == "cmd"):
            raise CompileError("raw pages only contain cmd(..., indent=N) lines", stmt)
        cmd, indent = K.generic_encode(call, ctx)
        cmd.indent = indent or 0
        out.append(cmd)
    return out


def compile_body_src(lines: list[str], ctx: Ctx) -> list[Command]:
    first = next((l for l in lines if l.strip()), "")
    cut = len(first) - len(first.lstrip(" "))
    src = "\n".join(l[cut:] for l in lines)
    saved = ctx.comments
    ctx.comments = CommentIndex(src)
    try:
        return compile_statements(ast.parse(src).body, ctx, 0, 0)
    finally:
        ctx.comments = saved


# --------------------------------------------------------------------------
# Page properties
# --------------------------------------------------------------------------

TRIGGERS = ["action", "touch", "collision", "autorun", "parallel"]
LAYERS = ["below", "same", "above"]
MOVE_TYPES = ["stationary", "random", "vertical", "horizontal", "toward_player", "away_from_player", "custom"]
PATTERNS = ["left", "middle", "right", "middle2"]
ANIMATIONS = [
    "non_continuous",
    "continuous",
    "fixed_non_continuous",
    "fixed_continuous",
    "fixed_graphic",
    "spin",
    "step_frame_fix",
]
CE_TRIGGERS = {3: "autorun", 4: "parallel", 5: "call"}

FLAG_BITS = {"switch_a": 1, "switch_b": 2, "variable": 4, "item": 8, "actor": 16, "timer": 32, "timer2": 64}

# Script defaults (what an omitted keyword means).
PAGE_DEFAULTS = {
    "direction": 2,
    "pattern": 1,
    "translucent": False,
    "move_type": 0,
    "frequency": 3,
    "trigger": 0,
    "layer": 0,
    "overlap_forbidden": False,
    "animation": 0,
    "speed": 3,
}


class PageSpec:
    """One event page: its properties (see page_props_from_struct) and commands.

    A plain class rather than a pydantic model, like Command: a map can hold tens of
    thousands of pages (28,800 in one TestGame map), and validating each one copied
    its property dict (~750 bytes a page)."""

    __slots__ = ("props", "commands", "id", "lineno")

    def __init__(
        self, props: dict[str, Any], commands: list[Command], id: int | None = None, lineno: int | None = None
    ):
        self.props = props
        self.commands = commands
        self.id = id
        self.lineno = lineno

    def key(self):
        return (tuple(sorted((k, _hashable(v)) for k, v in self.props.items())), tuple(c.key() for c in self.commands))

    def __eq__(self, other) -> bool:
        if not isinstance(other, PageSpec):
            return NotImplemented
        return (self.props, self.commands, self.id, self.lineno) == (
            other.props,
            other.commands,
            other.id,
            other.lineno,
        )

    __hash__ = None  # mutable, like its props

    def __repr__(self) -> str:
        return "PageSpec(props=%r, commands=<%d>, id=%r)" % (self.props, len(self.commands), self.id)


def _hashable(v):
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    if isinstance(v, MoveCommand):
        return v.key()
    return v


class EventSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int | None
    name: bytes
    x: int
    y: int
    pages: SkipValidation[list[PageSpec]]  # a plain class, already checked: not copied nor re-validated
    lineno: int | None = None

    def key(self):
        return (self.id, self.name, self.x, self.y, tuple(p.key() for p in self.pages))


class CommonEventSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int | None
    name: bytes
    trigger: int
    switch_id: int | None
    commands: SkipValidation[list[Command]]  # a plain class, already checked: not copied nor re-validated
    lineno: int | None = None

    def key(self):
        return (self.id, self.name, self.trigger, self.switch_id, tuple(c.key() for c in self.commands))


class KeepSpec(BaseModel):
    """An entry of an incremental compile whose source did not change since the
    last sync: the game's entry is left exactly as it is (not even decoded)."""

    id: int

    def key(self):
        return ("keep", self.id)


def split_entries(text: str, decorator: str, indent: str) -> tuple[list[str], dict[int, tuple[int, int]]] | None:
    """A script's lines, and id -> [first, end) line range of each `@decorator(id, ...)`
    entry written at `indent`.  An entry runs from its decorator through its body, up to
    the next line indented no deeper than the decorator.  None when an entry has no
    literal id or an id appears twice: the caller then compiles everything."""
    lines = text.split("\n")
    head = indent + "@" + decorator + "("
    starts = [n for n, line in enumerate(lines) if line.startswith(head)]
    entries: dict[int, tuple[int, int]] = {}
    for k, start in enumerate(starts):
        limit = starts[k + 1] if k + 1 < len(starts) else len(lines)
        end, body = start + 1, False
        while end < limit:
            line = lines[end]
            if not body and line.startswith(indent + "def "):
                body = True  # the decorator (maybe over several lines) ends here
            elif body and line.strip() and len(line) - len(line.lstrip()) <= len(indent):
                break
            end += 1
        if not body:
            return None
        m = re.match(r"\s*" + re.escape("@" + decorator) + r"\(\s*(\d+)\s*[,)]", "\n".join(lines[start:end]))
        if m is None or int(m.group(1)) in entries:
            return None
        entries[int(m.group(1))] = (start, end)
    return lines, entries


def _skeleton(lines: list[str], entries: dict[int, tuple[int, int]]) -> list[str]:
    """The lines outside the entries, each entry replaced by a marker."""
    out, pos = [], 0
    for eid, (a, b) in sorted(entries.items(), key=lambda kv: kv[1][0]):
        out += lines[pos:a] + ["\0%d" % eid]
        pos = b
    return out + lines[pos:]


def incremental_source(text: str, base: str, decorator: str, indent: str) -> tuple[str, list[int], list[int]] | None:
    """For a script edited since the last sync (`base`): the source to compile, with the
    unchanged entries blanked out (line numbers kept), the changed ids and the unchanged
    ids.  None when anything outside the entries changed: compile everything then."""
    cur, old = split_entries(text, decorator, indent), split_entries(base, decorator, indent)
    if cur is None or old is None:
        return None
    (lines, entries), (base_lines, base_entries) = cur, old
    if _skeleton(lines, entries) != _skeleton(base_lines, base_entries):
        return None
    changed = [
        eid for eid, (a, b) in entries.items() if lines[a:b] != base_lines[base_entries[eid][0] : base_entries[eid][1]]
    ]
    if not changed:
        return None
    part = list(lines)
    for eid, (a, b) in entries.items():
        if eid not in changed:
            part[a:b] = [""] * (b - a)
    return "\n".join(part), changed, [eid for eid in entries if eid not in changed]


def differs_by_blank_lines_only(text: str, other: str, decorator: str, indent: str) -> bool:
    """The same entries, and only blank lines differ outside them: a blank line between
    two entries changes no event (ruff adding one must not cost compiling the whole
    script).  Inside an entry a blank line can matter (it splits comments)."""
    a, b = split_entries(text, decorator, indent), split_entries(other, decorator, indent)
    if a is None or b is None:
        return False
    (lines, entries), (other_lines, other_entries) = a, b
    if set(entries) != set(other_entries) or any(
        lines[s:e] != other_lines[other_entries[eid][0] : other_entries[eid][1]] for eid, (s, e) in entries.items()
    ):
        return False
    skeleton = [l for l in _skeleton(lines, entries) if l.strip()]
    return skeleton == [l for l in _skeleton(other_lines, other_entries) if l.strip()]


def parse_by_entries(src: str, filename: str, decorator: str, indent: str) -> tuple[ast.Module, Callable]:
    """Parse a script one `@decorator(id, ...)` entry at a time.

    Python's syntax tree weighs ~90 times the source (170 MB for a 2 MB script), so
    the whole file is never parsed at once: the skeleton is parsed with each entry
    replaced by a `pass` on its first line, and `entry(stmt)` parses an entry when
    the compiler reaches its placeholder (its tree is dropped once compiled).  Line
    and column numbers are those of the whole file.  -> (skeleton, entry) where entry
    returns the statements an entry placeholder stands for, else [stmt].  Falls back
    to parsing the whole file (same errors as ast.parse) when the entries cannot be
    told apart reliably."""
    found = split_entries(src, decorator, indent)
    if found is not None and found[1]:
        lines, entries = found
        skeleton = list(lines)
        starts = {}
        for a, b in entries.values():
            skeleton[a:b] = [indent + "pass"] + [""] * (b - a - 1)
            starts[a + 1] = (a, b)  # line number of the placeholder
        try:
            tree = ast.parse("\n".join(skeleton), filename)

            def entry(stmt: ast.stmt) -> list[ast.stmt]:
                if not (isinstance(stmt, ast.Pass) and stmt.lineno in starts):
                    return [stmt]
                a, b = starts[stmt.lineno]
                body = "\n".join(lines[a:b])
                if not indent:
                    return ast.parse("\n" * a + body, filename).body
                # an entry inside a class: parsed in a class of its own on the line before
                return ast.parse("\n" * (a - 1) + "class _:\n" + body, filename).body[0].body  # type: ignore[attr-defined]

            return tree, entry
        except SyntaxError:
            pass  # not split where Python would: parse it whole for the real error
    return ast.parse(src, filename), lambda stmt: [stmt]


def page_props_from_struct(p: Struct) -> dict[str, Any]:
    cond = p.get("condition")
    flags = cond.get("flags")
    c = {}
    if flags & 1:
        c["switch_a"] = cond.get("switch_a_id")
    if flags & 2:
        c["switch_b"] = cond.get("switch_b_id")
    if flags & 4:
        c["variable"] = (cond.get("variable_id"), cond.get("compare_operator"), cond.get("variable_value"))
    if flags & 8:
        c["item"] = cond.get("item_id")
    if flags & 16:
        c["actor"] = cond.get("actor_id")
    if flags & 32:
        c["timer"] = cond.get("timer_sec")
    if flags & 64:
        c["timer2"] = cond.get("timer2_sec")
    route = p.get("move_route")
    return {
        "condition": c,
        "graphic": (bytes(p.get("character_name")), p.get("character_index")),
        "direction": p.get("character_direction"),
        "pattern": p.get("character_pattern"),
        "translucent": p.get("translucent"),
        "move_type": p.get("move_type"),
        "frequency": p.get("move_frequency"),
        "trigger": p.get("trigger"),
        "layer": p.get("layer"),
        "overlap_forbidden": p.get("overlap_forbidden"),
        "animation": p.get("animation_type"),
        "speed": p.get("move_speed"),
        "route": (list(route.get("move_commands")), route.get("repeat"), route.get("skippable")),
    }


def page_kwargs_src(props: dict[str, Any], ctx: Ctx) -> list[tuple[str, Any]]:
    kw: list[tuple[str, Any]] = []
    c = props["condition"]
    second_switch_only = "switch_b" in c and "switch_a" not in c  # editor's 2nd switch slot, 1st unused
    when = P.page_condition_src({k: v for k, v in c.items() if k != "switch_b"} if second_switch_only else c)
    if when is not None or second_switch_only:
        if when is not None:
            kw.append(("when", Raw(src=when)))
        if second_switch_only:
            kw.append(("switch_b", c["switch_b"]))
    else:  # unusual combinations keep the explicit form
        for k in ("switch_a", "switch_b"):
            if k in c:
                kw.append((k, c[k]))
        if "variable" in c:
            vid, op, val = c["variable"]
            kw.append(("variable", (vid, K.enum_name(P.COMPARE, op), val)))
        for k in ("item", "actor", "timer", "timer2"):
            if k in c:
                kw.append((k, c[k]))
    name, index = props["graphic"]
    if name:
        kw.append(("sprite", (ctx.dec(name), index)))
    elif index:
        kw.append(("tile", index))
    enums = {
        "direction": K.DIRECTIONS,
        "pattern": PATTERNS,
        "move_type": MOVE_TYPES,
        "trigger": TRIGGERS,
        "layer": LAYERS,
        "animation": ANIMATIONS,
    }
    for k in (
        "trigger",
        "direction",
        "pattern",
        "translucent",
        "layer",
        "overlap_forbidden",
        "animation",
        "move_type",
        "frequency",
        "speed",
    ):
        v = props[k]
        if v != PAGE_DEFAULTS[k]:
            kw.append((k, K.enum_name(enums[k], v) if k in enums else v))
    moves, repeat, skippable = props["route"]
    if moves or not repeat or skippable:
        rkw = []
        if not repeat:
            rkw.append(("repeat", False))
        if skippable:
            rkw.append(("skippable", True))
        kw.append(("route", Raw(src=render_call("route", K.moves_to_src(moves, ctx), rkw))))
    return kw


def page_props_from_call(call: Call, ctx: Ctx) -> tuple[dict[str, Any], int | None, bool]:
    """-> (props, explicit page id, raw flag)"""
    legacy = {"switch_a", "switch_b", "variable", "item", "actor", "timer", "timer2"}
    allowed = {"id", "raw", "when", "sprite", "tile", "route"} | legacy | set(PAGE_DEFAULTS)
    if call.args:
        raise CompileError("page() only takes keyword arguments", call.node)
    for k in call.kwargs:
        if k not in allowed:
            raise CompileError(
                "page() got an unknown argument %r (expected one of: %s)" % (k, ", ".join(sorted(allowed))), call.node
            )
    a = K.Args(call, sorted(allowed))
    cond = {}
    if a.get("when") is not None:
        if (legacy - {"switch_b"}) & set(call.kwargs):
            raise CompileError("page(): use either when=... or switch_a=/variable=/..., not both", call.node)
        cond = P.page_condition_val(a.get("when"), ctx)
        if "switch_b" in cond and a.get("switch_b") is not None:
            raise CompileError("page(): a page has two switch slots; when= already uses both", call.node)
    for k in ("switch_a", "switch_b", "item", "actor", "timer", "timer2"):
        if a.get(k) is not None:
            cond[k] = a.int(k)
    if a.get("variable") is not None:
        v = a.get("variable")
        if isinstance(v, tuple) and len(v) == 2:
            v = (v[0], ">=", v[1])
        if not (isinstance(v, tuple) and len(v) == 3 and isinstance(v[0], int) and isinstance(v[2], int)):
            raise CompileError('page(): variable must be (variable_id, ">=", value)', call.node)
        op = v[1]
        if isinstance(op, str):
            if op not in P.COMPARE:
                raise CompileError("page(): unknown comparison %r" % op, call.node)
            op = P.COMPARE.index(op)
        cond["variable"] = (v[0], op, v[2])
    graphic = (b"", 0)
    if a.get("sprite") is not None:
        s = a.get("sprite")
        if not (isinstance(s, tuple) and len(s) == 2 and isinstance(s[0], str) and isinstance(s[1], int)):
            raise CompileError('page(): sprite must be ("CharSetName", index)', call.node)
        graphic = (ctx.enc(s[0], call.node), s[1])
    elif a.get("tile") is not None:
        graphic = (b"", a.int("tile"))
    props = {
        "condition": cond,
        "graphic": graphic,
        "direction": a.enum("direction", K.DIRECTIONS, PAGE_DEFAULTS["direction"]),
        "pattern": a.enum("pattern", PATTERNS, PAGE_DEFAULTS["pattern"]),
        "translucent": a.bool("translucent", False),
        "move_type": a.enum("move_type", MOVE_TYPES, PAGE_DEFAULTS["move_type"]),
        "frequency": a.int("frequency", PAGE_DEFAULTS["frequency"]),
        "trigger": a.enum("trigger", TRIGGERS, PAGE_DEFAULTS["trigger"]),
        "layer": a.enum("layer", LAYERS, PAGE_DEFAULTS["layer"]),
        "overlap_forbidden": a.bool("overlap_forbidden", False),
        "animation": a.enum("animation", ANIMATIONS, PAGE_DEFAULTS["animation"]),
        "speed": a.int("speed", PAGE_DEFAULTS["speed"]),
    }
    route = a.get("route")
    if route is None:
        props["route"] = ([], True, False)
    else:
        if not K.is_call(route, "route"):
            raise CompileError("page(): route must be route(move_up, ..., repeat=True)", call.node)
        ra = dict(route.kwargs)
        for k in ra:
            if k not in ("repeat", "skippable"):
                raise CompileError("route() got an unknown argument %r" % k, route.node)
        props["route"] = (
            K.moves_from_values(route.args, ctx, route.node),
            bool(ra.get("repeat", True)),
            bool(ra.get("skippable", False)),
        )
    pid = a.get("id")
    return props, pid, a.bool("raw", False)


def apply_page_props(p: Struct, props: dict[str, Any]) -> None:
    """Patch a page struct so it carries `props`, touching only what differs."""

    def put(s: Struct, key, value):
        if s.get(key) != value:
            s.set(key, value)

    cur = page_props_from_struct(p)
    if cur["condition"] != props["condition"]:
        cond = p.get("condition") if p.has("condition") else Struct("EventPageCondition")
        c = props["condition"]
        flags = cond.get("flags") & ~0x7F
        for k, bit in FLAG_BITS.items():
            if k in c:
                flags |= bit
        put(cond, "flags", flags)
        for k, field_name in (
            ("switch_a", "switch_a_id"),
            ("switch_b", "switch_b_id"),
            ("item", "item_id"),
            ("actor", "actor_id"),
            ("timer", "timer_sec"),
            ("timer2", "timer2_sec"),
        ):
            if k in c:
                put(cond, field_name, c[k])
        if "variable" in c:
            vid, op, val = c["variable"]
            put(cond, "variable_id", vid)
            put(cond, "variable_value", val)
            put(cond, "compare_operator", op)
        p.set("condition", cond)
    name, index = props["graphic"]
    if cur["graphic"] != props["graphic"]:
        if name or p.has("character_name"):
            put(p, "character_name", name)
        put(p, "character_index", index)
    for key, field_name in (
        ("direction", "character_direction"),
        ("pattern", "character_pattern"),
        ("translucent", "translucent"),
        ("move_type", "move_type"),
        ("frequency", "move_frequency"),
        ("trigger", "trigger"),
        ("layer", "layer"),
        ("overlap_forbidden", "overlap_forbidden"),
        ("animation", "animation_type"),
        ("speed", "move_speed"),
    ):
        if cur[key] != props[key]:
            p.set(field_name, props[key])
    if cur["route"] != props["route"]:
        route = p.get("move_route") if p.has("move_route") else Struct("MoveRoute")
        moves, repeat, skippable = props["route"]
        if list(route.get("move_commands")) != moves:
            route.set("move_commands", list(moves))
        put(route, "repeat", repeat)
        put(route, "skippable", skippable)
        p.set("move_route", route)


def new_page_struct() -> Struct:
    """An empty page laid out like the RPG Maker editor writes a new one."""
    p = Struct("EventPage")
    cond = Struct("EventPageCondition")
    cond.set("flags", 0)
    cond.set("actor_id", 1)
    p.set("condition", cond)
    p.set("character_direction", 2)
    p.set("translucent", False)
    p.set("move_type", 0)
    p.set("trigger", 0)
    p.set("layer", 0)
    p.set("overlap_forbidden", False)
    p.set("animation_type", 0)
    route = Struct("MoveRoute")
    route.set("move_commands", [])
    p.set("move_route", route)
    p.set("event_commands", CommandList([]))
    return p


# --------------------------------------------------------------------------
# File level: maps
# --------------------------------------------------------------------------

HEADER_NOTE = """\
# Edit freely: changes are written back to {target} by `rpgsync watch`,
# only once the script compiles and fits the engine (see tests/README.md).
# Full-line '#' comments inside events are RPG Maker comments (a blank line
# starts a new one); comments at the end of a code line are not kept."""


_GENERIC_NAME = re.compile(r"^(EV|CE)?\d*$", re.I)


def _slug(name: str) -> str:
    """Python identifier from an event name: "Haru Intro" -> haru_intro."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ascii_name)
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")


# Event scripts take variables / switches from the database files, so that
# ``variables.code_voulu`` is the attribute defined in database/variables.py.
DSL_IMPORT = """from rpgsync.dsl import *
from database.common_events import common_events
from database.switches import switches
from database.variables import variables"""
CE_IMPORT = """from rpgsync.dsl import *
from database.switches import switches
from database.variables import variables"""


def _dsl_import(ctx: Ctx) -> str:
    return DSL_IMPORT


@functools.lru_cache(maxsize=4)  # a sync reads the same (large) script more than once
def _entry_function_names(text: str, decorator: str, indent: str) -> dict[int, str] | None:
    """id -> function name read from the `def` lines of the entries, without parsing
    the script (its syntax tree weighs ~90 times the text); None when they cannot be
    read that way (an entry without a literal id...)."""
    found = split_entries(text, decorator, indent)
    if found is None:
        return None
    lines, entries = found
    out = {}
    for eid, (a, b) in entries.items():
        m = next((re.match(r"\s*def (\w+)\(", l) for l in lines[a:b] if l.lstrip().startswith("def ")), None)
        if m is None:
            return None
        out[eid] = m.group(1)
    return out


def read_function_names(text: str, decorator: str = "common_event") -> dict[int, str]:
    """id -> function name of the @common_event functions of a script (to keep
    the names when it is rewritten).  Don't modify the returned dict."""
    for indent in (INDENT, ""):  # in the class, or at the top level (older files)
        names = _entry_function_names(text, decorator, indent)
        if names:
            return names
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == decorator:
                    first = d.args[0] if d.args else next((k.value for k in d.keywords if k.arg == "id"), None)
                    if isinstance(first, ast.Constant) and type(first.value) is int:
                        out[first.value] = node.name
    return out


def common_event_names(
    names: dict[int, str], keep: dict[int, str] | None = None, prefix: str = "ce", table: type | None = None
) -> dict[int, str]:
    """Function names of the common events (id -> name): the ones in the
    file (`keep`), else derived from the editor names, else ce_<id>.  Also
    names the troops of troop_events.py (prefix "troop", their table class)."""
    from . import dsl

    reserved = set(dir(dsl)) | set(dir(table or dsl.CommonEventTable)) | set(keyword.kwlist) | {"size", "page"}
    out: dict[int, str] = {}
    used: set[str] = set()
    for n, fname in (keep or {}).items():
        if n in names and fname.isidentifier() and fname not in reserved and fname not in used:
            out[n] = fname
            used.add(fname)
    for n, name in names.items():
        if n in out:
            continue
        slug = "" if _GENERIC_NAME.match(name.strip()) else _slug(name)
        if not slug or slug[0].isdigit() or slug in reserved:
            slug = "%s_%d" % (prefix, n)
        while slug in used:
            slug = "%s_%d" % (slug, n)
        out[n] = slug
        used.add(slug)
    return out


def function_names(items: list[ArrayItem], ctx: Ctx, prefix: str) -> dict[int, str]:
    """Readable, unique function names for events / common events."""
    from . import dsl

    reserved = set(dir(dsl)) | set(keyword.kwlist) | {"page"}
    slugs = {}
    for it in items:
        name = ctx.dec(it.struct.get("name")).strip()
        slug = "" if _GENERIC_NAME.match(name) else _slug(name)
        if not slug or slug[0].isdigit() or slug in reserved:
            slug = ""
        slugs[it.id] = slug
    counts: dict[str, int] = {}
    for slug in slugs.values():
        counts[slug] = counts.get(slug, 0) + 1
    out = {}
    for iid, slug in slugs.items():
        if not slug:
            out[iid] = "%s_%d" % (prefix, iid)
        elif counts[slug] > 1:
            out[iid] = "%s_%d" % (slug, iid)
        else:
            out[iid] = slug
    return out


def decompile_map(m: Struct, ctx: Ctx, title: str, target: str) -> str:
    out = ['"""%s"""' % title.replace('"""', "'''"), HEADER_NOTE.format(target=target), _dsl_import(ctx)]
    for lines in decompile_map_events(m, ctx).values():
        out.append("")
        out.append("")
        out.extend(lines)
    return "\n".join(out).rstrip() + "\n"


def decompile_map_events(m: Struct, ctx: Ctx, ids: set[int] | None = None) -> dict[int, list[str]]:
    """id -> script lines of the map's events (only of `ids` when given), in map order.
    Each event is named and refers to the others exactly as in the whole script."""
    items = m.get("events")
    names = function_names(items, ctx, "ev")
    ctx.event_names = {
        it.id: ctx.dec(it.struct.get("name"))
        for it in items
        if not _GENERIC_NAME.match(ctx.dec(it.struct.get("name")).strip())
    }
    # named events are referred to by their function name: haru.move(...)
    handles = {eid: names[eid] for eid in ctx.event_names if not names[eid].startswith("ev_")}
    out = {}
    try:
        with P.map_events(names=handles, ids={name: eid for eid, name in handles.items()}), P.db_names(ctx):
            for item in items:
                if ids is None or item.id in ids:
                    out[item.id] = decompile_event(item, ctx, names[item.id])
                    item.struct.release()  # one event's decoded pages at a time
    finally:
        ctx.event_names = {}
    return out


def decompile_event(item: ArrayItem, ctx: Ctx, fname: str | None = None) -> list[str]:
    e = item.struct
    lines = [
        "@event(%d, %s, x=%d, y=%d)" % (item.id, K.pystr(ctx.dec(e.get("name"))), e.get("x"), e.get("y")),
        "def %s():" % (fname or "ev_%d" % item.id),
    ]
    pages = e.get("pages")
    for n, pg in enumerate(pages, 1):
        props = page_props_from_struct(pg.struct)
        kw = page_kwargs_src(props, ctx)
        if pg.id != n:
            kw.insert(0, ("id", pg.id))
        body, structured = body_lines(list(pg.struct.get("event_commands")), ctx, 2)
        if not structured:
            kw.append(("raw", True))
        if n > 1:
            lines.append("")
        lines.append(INDENT + "@" + render_call("page", [], kw))
        lines.append(INDENT + "def page_%d():" % n)
        lines.extend(_body(body, INDENT * 2))
    if not pages:
        lines.append(INDENT + "pass")
    return lines


def _decorator(fn: ast.FunctionDef, name: str) -> Call:
    calls = [evaluate(d) for d in fn.decorator_list]
    calls = [c for c in calls if isinstance(c, Call) and c.name == name]
    if len(calls) != 1:
        raise CompileError("function %s needs exactly one @%s(...) decorator" % (fn.name, name), fn)
    return calls[0]


def compile_map_source(src: str, ctx: Ctx, filename: str = "<script>", names_src: str | None = None) -> list[EventSpec]:
    """names_src: the whole script, when `src` holds only some of its events (incremental
    compile): the events refer to each other by function name."""
    with P.map_events(ids=_event_function_ids(names_src or src)), P.db_names(ctx):
        return _compile_map_source(src, ctx, filename)


def _event_function_ids(src: str) -> dict[str, int | None]:
    """Function name -> id of the @event functions of a map script."""
    found = split_entries(src, "event", "")
    if found is not None:  # every entry has a literal id: read the def lines only
        lines, entries = found
        out: dict[str, int | None] = {}
        for eid, (a, b) in entries.items():
            m = next((re.match(r"def (\w+)\(", l) for l in lines[a:b] if l.startswith("def ")), None)
            if m is None:
                break
            out[m.group(1)] = eid
        else:
            return out
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}  # reported by the compiler
    out: dict[str, int | None] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.FunctionDef):
            continue
        for d in stmt.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "event":
                arg = d.args[0] if d.args else next((k.value for k in d.keywords if k.arg == "id"), None)
                ok = isinstance(arg, ast.Constant) and type(arg.value) is int
                out[stmt.name] = arg.value if ok else None  # type: ignore[union-attr]
    return out


def _compile_map_source(src: str, ctx: Ctx, filename: str = "<script>") -> list[EventSpec]:
    try:
        tree, entry = parse_by_entries(src, filename, "event", "")
        statements = (s for placeholder in tree.body for s in entry(placeholder))
        events = _compile_map_statements(statements, src, ctx)
    except SyntaxError as e:
        raise CompileError("syntax error: %s" % e.msg, e) from None
    return events


def _compile_map_statements(statements: Iterable[ast.stmt], src: str, ctx: Ctx) -> list[EventSpec]:
    ctx.comments = CommentIndex(src)
    events: list[EventSpec] = []
    seen = {}
    for stmt in statements:
        if _is_header_stmt(stmt):
            continue
        if not isinstance(stmt, ast.FunctionDef):
            raise CompileError("top level may only contain @event functions", stmt)
        dec = _decorator(stmt, "event")
        a = K.Args(dec, ["id", "name", "x", "y"])
        eid = a.get("id")
        if eid is not None and (not isinstance(eid, int) or eid < 1):
            raise CompileError("event id must be a positive integer", stmt)
        if eid is not None and eid in seen:
            raise CompileError("event id %d is used twice (also on line %d)" % (eid, seen[eid]), stmt)
        if eid is not None:
            seen[eid] = stmt.lineno
        pages = []
        for sub in stmt.body:
            if isinstance(sub, ast.Pass) or (isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Constant)):
                continue
            if not isinstance(sub, ast.FunctionDef):
                raise CompileError("an @event function may only contain @page functions", sub)
            pcall = _decorator(sub, "page")
            props, pid, raw = page_props_from_call(pcall, ctx)
            if raw:
                cmds = compile_raw_statements(sub.body, ctx)
            else:
                cmds = compile_statements(sub.body, ctx, 0, sub.lineno)
            pages.append(PageSpec(props=props, commands=cmds, id=pid, lineno=sub.lineno))
        events.append(
            EventSpec(
                id=eid,
                name=ctx.enc(a.str("name", ""), stmt),
                x=a.int("x", 0),
                y=a.int("y", 0),
                pages=pages,
                lineno=stmt.lineno,
            )
        )
    return events


def _is_header_stmt(stmt) -> bool:
    return isinstance(stmt, (ast.Import, ast.ImportFrom)) or (
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)
    )


def map_event_specs(m: Struct, ctx: Ctx, ids: set | None = None) -> list[EventSpec]:
    """Specs straight from binary data (equal to compiling the decompiled text);
    only of `ids` when given."""
    out = []
    for item in m.get("events"):
        if ids is not None and item.id not in ids:
            continue
        e = item.struct
        pages = [
            PageSpec(
                props=page_props_from_struct(pg.struct),
                commands=list(pg.struct.get("event_commands")),
                id=pg.id if pg.id != n else None,
            )
            for n, pg in enumerate(e.get("pages"), 1)
        ]
        out.append(EventSpec(id=item.id, name=bytes(e.get("name")), x=e.get("x"), y=e.get("y"), pages=pages))
    return out


def assign_ids(specs, start_ids=()) -> list[tuple[Any, int]]:
    """Give spec objects without an id the lowest free id.  -> [(spec, id)]"""
    used = {s.id for s in specs if s.id is not None} | set(start_ids)
    assigned = []
    nxt = 1
    for s in specs:
        if s.id is None:
            while nxt in used:
                nxt += 1
            s.id = nxt
            used.add(nxt)
            assigned.append((s, nxt))
    return assigned


def apply_events(m: Struct, specs: list[EventSpec]) -> dict[str, list[int]]:
    """Patch the map's event array to match specs.  Returns a change summary."""
    current = {it.id: it for it in m.get("events")}
    summary = {"added": [], "removed": [], "changed": []}
    new_items = []
    for spec in sorted(specs, key=lambda s: s.id):
        item = current.get(spec.id)
        if isinstance(spec, KeepSpec):
            if item is None:
                raise ValueError("event %d is not on the map any more" % spec.id)
            new_items.append(item)  # unchanged since the last sync: left as it is
            continue
        if item is None:
            st = Struct("Event")
            st.set("name", spec.name)
            st.set("x", spec.x)
            st.set("y", spec.y)
            st.set("pages", [])
            item = ArrayItem(id=spec.id, struct=st)
            summary["added"].append(spec.id)
        elif _patch_event(item.struct, spec):
            summary["changed"].append(spec.id)
        if spec.id in summary["added"]:
            _patch_event(item.struct, spec)
        new_items.append(item)
    summary["removed"] = sorted(set(current) - {s.id for s in specs})
    if (
        summary["added"]
        or summary["removed"]
        or summary["changed"]
        or [it.id for it in m.get("events")] != [it.id for it in new_items]
    ):
        m.set("events", new_items)
    return summary


def _patch_event(e: Struct, spec: EventSpec) -> bool:
    changed = False
    for key, val in (("name", spec.name), ("x", spec.x), ("y", spec.y)):
        if e.get(key) != val:
            e.set(key, val)
            changed = True
    pages = list(e.get("pages"))
    new_pages = []
    for n, ps in enumerate(spec.pages, 1):
        pid = ps.id if ps.id is not None else n
        if n - 1 < len(pages):
            item = pages[n - 1]
            if item.id != pid:
                item = ArrayItem(id=pid, struct=item.struct)
        else:
            item = ArrayItem(id=pid, struct=new_page_struct())
        if _patch_page(item.struct, ps):
            changed = True
        new_pages.append(item)
    if [(p.id, id(p.struct)) for p in new_pages] != [(p.id, id(p.struct)) for p in pages]:
        changed = True
        e.set("pages", new_pages)
    elif changed:
        e.mark_dirty("pages")
    return changed


def _patch_page(p: Struct, ps: PageSpec) -> bool:
    changed = False
    if page_props_from_struct(p) != ps.props:
        apply_page_props(p, ps.props)
        changed = True
    if list(p.get("event_commands")) != ps.commands:
        p.set("event_commands", CommandList(ps.commands))
        changed = True
    return changed


# --------------------------------------------------------------------------
# File level: common events
# --------------------------------------------------------------------------


def decompile_common_events(db: Struct, ctx: Ctx, target: str) -> str:
    with P.db_names(ctx):
        return _decompile_common_events(db, ctx, target)


def _decompile_common_events(db: Struct, ctx: Ctx, target: str) -> str:
    out = ['"""Common events"""', "", HEADER_NOTE.format(target=target), CE_IMPORT, "", ""]
    out += ["class CommonEvents(CommonEventTable):", *K.config_src(len(db.get("commonevents")), INDENT)]
    for lines in _decompile_common_event_entries(db, ctx).values():
        out.append("")
        out.extend(lines)
    out += ["", "", "common_events = CommonEvents()"]
    return "\n".join(out).rstrip() + "\n"


def decompile_common_event_entries(db: Struct, ctx: Ctx, ids: set[int] | None = None) -> dict[int, list[str]]:
    """id -> script lines of the used common events (only of `ids` when given), in order."""
    with P.db_names(ctx):
        return _decompile_common_event_entries(db, ctx, ids)


def _decompile_common_event_entries(db: Struct, ctx: Ctx, ids: set[int] | None = None) -> dict[int, list[str]]:
    names = ctx.handles.get("common_events") or common_event_names(_ce_names(db, ctx))
    out = {}
    for item in db.get("commonevents"):
        if ids is not None and item.id not in ids:
            continue
        if is_empty_common_event(_ce_spec(item)):
            continue  # unused slot; scripts only list used common events
        ce = item.struct
        kw = [("trigger", CE_TRIGGERS.get(ce.get("trigger"), ce.get("trigger")))]
        if ce.get("switch_flag"):
            kw.append(("switch", ce.get("switch_id")))
        body, structured = body_lines(list(ce.get("event_commands")), ctx, 2)
        if not structured:
            kw.append(("raw", True))
        lines = [INDENT + "@" + render_call("common_event", [item.id, ctx.dec(ce.get("name"))], kw)]
        lines.append(INDENT + "def %s():" % names[item.id])
        lines.extend(_body(body, INDENT * 2))
        out[item.id] = lines
        item.struct.release()  # one common event's decoded commands at a time
    return out


def _ce_names(db: Struct, ctx: Ctx) -> dict[int, str]:
    """id -> editor name of the used common events."""
    return {item.id: ctx.dec(item.struct.get("name")) for item in db.get("commonevents") if not _unused_slot(item)}


def _unused_slot(item: ArrayItem) -> bool:
    """is_empty_common_event, cheap fields first: a named event's commands are never decoded."""
    ce = item.struct
    if ce.get("name") or ce.get("trigger") != 5 or ce.get("switch_flag"):
        return False
    return not list(ce.get("event_commands"))


def compile_common_events_source(src: str, ctx: Ctx, filename: str = "<script>") -> list:
    with P.db_names(ctx):
        return _compile_common_events_source(src, ctx, filename)


def _compile_common_events_source(src: str, ctx: Ctx, filename: str = "<script>") -> list:
    try:
        tree, entry = parse_by_entries(src, filename, "common_event", INDENT)
        return _compile_common_event_statements(tree, entry, src, ctx)
    except SyntaxError as e:
        raise CompileError("syntax error: %s" % e.msg, e) from None


def _compile_common_event_statements(tree: ast.Module, entry: Callable, src: str, ctx: Ctx) -> list:
    ctx.comments = CommentIndex(src)
    out = []
    seen = {}
    triggers = {v: k for k, v in CE_TRIGGERS.items()}
    classes = [st for st in tree.body if isinstance(st, ast.ClassDef)]
    if len(classes) > 1:
        raise CompileError("common_events.py holds one class, CommonEvents", classes[1])
    body = list(tree.body)
    if classes:  # the @common_event functions are in the class (older files have them at the top level)
        for stmt in tree.body:
            if _is_header_stmt(stmt) or stmt is classes[0]:
                continue
            v = stmt.value if isinstance(stmt, ast.Assign) else None
            if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == classes[0].name):
                raise CompileError(
                    "top level may only contain `class CommonEvents(...)` and `common_events = CommonEvents()`", stmt
                )
        body = classes[0].body
    for stmt in (s for placeholder in body for s in entry(placeholder) if not isinstance(s, ast.Pass)):
        if _is_header_stmt(stmt):
            continue
        table_size = size_stmt(stmt)
        if table_size is not None:
            if any(isinstance(s, TableSize) for s in out):
                raise CompileError("size is assigned twice", stmt)
            out.append(table_size)
            continue
        if not isinstance(stmt, ast.FunctionDef):
            raise CompileError("CommonEvents may only contain `size = N` and @common_event functions", stmt)
        dec = _decorator(stmt, "common_event")
        a = K.Args(dec, ["id", "name", "trigger", "switch", "raw"])
        cid = a.get("id")
        if cid is not None and (not isinstance(cid, int) or cid < 1):
            raise CompileError("common event id must be a positive integer", stmt)
        if cid in seen:
            raise CompileError("common event id %d is used twice (also on line %d)" % (cid, seen[cid]), stmt)
        if cid is not None:
            seen[cid] = stmt.lineno
        trig = a.get("trigger", "call")
        if isinstance(trig, str):
            if trig not in triggers:
                raise CompileError("common_event(): trigger must be call, autorun or parallel", stmt)
            trig = triggers[trig]
        sw = a.get("switch")
        if a.bool("raw"):
            cmds = compile_raw_statements(stmt.body, ctx)
        else:
            cmds = compile_statements(stmt.body, ctx, 0, stmt.lineno)
        out.append(
            CommonEventSpec(
                id=cid,
                name=ctx.enc(a.str("name", ""), stmt),
                trigger=trig,
                switch_id=sw if sw is None else a.int("switch"),
                commands=cmds,
                lineno=stmt.lineno,
            )
        )
    return out


def common_event_specs(db: Struct) -> list[CommonEventSpec]:
    """Specs of the used common events (empty slots are not listed)."""
    specs = [_ce_spec(item) for item in db.get("commonevents")]
    return [s for s in specs if not is_empty_common_event(s)]


def is_empty_common_event(spec: CommonEventSpec) -> bool:
    """An unused database slot (no name, no commands, called, no switch)."""
    return not spec.name and not spec.commands and spec.trigger == 5 and spec.switch_id is None


def new_common_event_struct() -> Struct:
    """An empty slot laid out like the RPG Maker editor writes it."""
    st = Struct("CommonEvent")
    st.set("trigger", 5)
    st.set("event_commands", CommandList([]))
    return st


def apply_common_events(db: Struct, specs: list) -> dict[str, list[int]]:
    """Patch the database's common events to match specs.

    RPG_RT and EasyRPG look database entries up by position (id N is entry
    N), so the array never gets holes: a common event missing from the
    script is emptied in place, and new ids past the end are preceded by
    empty slots."""
    specs, wanted = split_size(specs)
    current = list(db.get("commonevents"))
    if any(it.id != n for n, it in enumerate(current, 1)):
        raise ValueError("the database's common events are not numbered 1..N; refusing to modify them")
    by_id = {s.id: s for s in specs}
    size = table_length(len(current), wanted, list(by_id))
    summary = {"added": [], "removed": [], "changed": []}
    items = []
    for cid in range(1, size + 1):
        item = current[cid - 1] if cid <= len(current) else None
        if isinstance(by_id.get(cid), KeepSpec):
            if item is None:
                raise ValueError("common event %d is not in the database any more" % cid)
            items.append(item)  # unchanged since the last sync: left as it is
            continue
        spec = by_id.get(cid) or CommonEventSpec(id=cid, name=b"", trigger=5, switch_id=None, commands=[])
        if item is None:
            item = ArrayItem(id=cid, struct=new_common_event_struct())
            if cid in by_id:
                summary["added"].append(cid)
        elif cid not in by_id and not _unused_slot(item):
            summary["removed"].append(cid)
        ce = item.struct
        changed = False
        for key, val in (("name", spec.name), ("trigger", spec.trigger), ("switch_flag", spec.switch_id is not None)):
            if ce.get(key) != val:
                ce.set(key, val)
                changed = True
        if spec.switch_id is not None and ce.get("switch_id") != spec.switch_id:
            ce.set("switch_id", spec.switch_id)
            changed = True
        if list(ce.get("event_commands")) != spec.commands:
            ce.set("event_commands", CommandList(spec.commands))
            changed = True
        if changed and cid in by_id and cid not in summary["added"]:
            summary["changed"].append(cid)
        items.append(item)
    if size != len(current):
        summary["size"] = [len(current), size]
    if summary["added"] or summary["removed"] or summary["changed"] or len(items) != len(current):
        db.set("commonevents", items)
    return summary


def _ce_spec(item: ArrayItem) -> CommonEventSpec:
    ce = item.struct
    return CommonEventSpec(
        id=item.id,
        name=bytes(ce.get("name")),
        trigger=ce.get("trigger"),
        switch_id=ce.get("switch_id") if ce.get("switch_flag") else None,
        commands=list(ce.get("event_commands")),
    )


# --------------------------------------------------------------------------
# File level: troop battle events
# --------------------------------------------------------------------------
#
# The battle events of the monster groups (Database > Troops > battle events),
# in database/troop_events.py:
#
#     class TroopEvents(TroopEventTable):
#         @troop(3, "Slime*2")
#         def slime_2():
#             @page(when=turn(1, every=2) and enemies[0].hp_percent(0, 50))
#             def page_1():
#                 text("The slime is weak!")
#
# The database keeps Troop.pages as a raw chunk (database/troops.py leaves it
# alone): only this file decodes it, and only that chunk is written back.

TROOP_HEADER = '"""Troop battle events: the battle event pages of each monster group (troops.py has the rest)"""'

# condition -> (bit of the flags, TroopPageCondition fields holding its values)
TROOP_CONDITIONS: dict[str, tuple[int, tuple[str, ...]]] = {
    "switch_a": (0, ("switch_a_id",)),
    "switch_b": (1, ("switch_b_id",)),
    "variable": (2, ("variable_id", "variable_value")),
    "turn": (3, ("turn_a", "turn_b")),
    "fatigue": (4, ("fatigue_min", "fatigue_max")),
    "enemy_hp": (5, ("enemy_id", "enemy_hp_min", "enemy_hp_max")),
    "actor_hp": (6, ("actor_id", "actor_hp_min", "actor_hp_max")),
    "turn_enemy": (7, ("turn_enemy_id", "turn_enemy_a", "turn_enemy_b")),  # RPG Maker 2003
    "turn_actor": (8, ("turn_actor_id", "turn_actor_a", "turn_actor_b")),  # RPG Maker 2003
    "command_actor": (9, ("command_actor_id", "command_id")),  # RPG Maker 2003
}
TROOP_CONDITIONS_2K3 = ("turn_enemy", "turn_actor", "command_actor")


class TroopSpec(BaseModel):
    """The battle event pages of one troop.  Its name and monsters belong to
    database/troops.py: the name in @troop(...) is a label, not part of the entry."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int | None
    pages: SkipValidation[list[PageSpec]]  # props: {"condition": {...}}, see troop_condition_from_struct
    lineno: int | None = None
    label: str | None = None  # the name written in @troop(id, "name"), and where (line, start, end column)
    label_at: tuple[int, int, int] | None = None

    def key(self):
        return (self.id, tuple(p.key() for p in self.pages))


def _label(dec: Call) -> tuple[str | None, tuple[int, int, int] | None]:
    """The name string of a @troop(id, "name") decorator and its place (UTF-8 columns)."""
    node = dec.node
    arg = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "name"), None)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.lineno == arg.end_lineno:
        return arg.value, (arg.lineno, arg.col_offset, arg.end_col_offset)
    return None, None


def fix_troop_labels(text: str, specs: list, names: dict[int, str]) -> str:
    """The script with the names of @troop(id, "name") set to the troops' names (`names`):
    they are labels, the troops are renamed in troops.py."""
    lines = text.split("\n")
    edits = [
        (s.label_at, K.pystr(names[s.id]))
        for s in specs
        if isinstance(s, TroopSpec) and s.label_at is not None and s.id in names and s.label != names[s.id]
    ]
    for (lineno, start, end), new in sorted(edits, reverse=True):
        raw = lines[lineno - 1].encode("utf-8")  # columns are in UTF-8 bytes
        lines[lineno - 1] = (raw[:start] + new.encode("utf-8") + raw[end:]).decode("utf-8")
    return "\n".join(lines)


def troop_pages(st: Struct) -> list[ArrayItem]:
    """A troop's battle event pages, decoded from its raw `pages` chunk ([] without one)."""
    data = st.get("pages", None)
    return decode_value("A:TroopPage", data) if data is not None else []


def troop_condition_from_struct(cond: Struct) -> dict[str, tuple[int, ...]]:
    """The conditions a page checks: key -> values (in TROOP_CONDITIONS field order).
    Values of unchecked conditions are not listed (the editor keeps them)."""
    flags = cond.get("flags")
    out = {}
    for key, (bit, fields) in TROOP_CONDITIONS.items():
        if bit // 8 < len(flags) and flags[bit // 8] >> (bit % 8) & 1:
            out[key] = tuple(cond.get(f) for f in fields)
    return out


def _unused_troop(pages: list[ArrayItem]) -> bool:
    """No battle events: no page, or the one empty page the editor gives a new troop."""
    if not pages:
        return True
    if len(pages) != 1 or pages[0].id != 1:
        return False
    p = pages[0].struct
    return not list(p.get("event_commands")) and not troop_condition_from_struct(p.get("condition"))


def _turn_src(head: str, start: int, every: int) -> str:
    return "%s(%d)" % (head, start) if every == 0 else "%s(%d, every=%d)" % (head, start, every)


def troop_condition_src(c: dict[str, tuple[int, ...]]) -> str | None:
    """`when=` of a troop page: the conditions joined with `and` (None: no condition)."""
    parts = []
    for key in TROOP_CONDITIONS:
        if key not in c:
            continue
        v = c[key]
        if key in ("switch_a", "switch_b"):
            parts.append(P.sw_src(v[0]))
        elif key == "variable":
            parts.append("%s >= %d" % (P.var_src(v[0]), v[1]))
        elif key == "turn":
            parts.append(_turn_src("turn", v[0], v[1]))
        elif key == "fatigue":
            parts.append("fatigue(%d, %d)" % v)
        elif key == "enemy_hp":
            parts.append("enemies[%d].hp_percent(%d, %d)" % v)
        elif key == "actor_hp":
            parts.append("actors[%d].hp_percent(%d, %d)" % v)
        elif key == "turn_enemy":
            parts.append(_turn_src("enemies[%d].turn" % v[0], v[1], v[2]))
        elif key == "turn_actor":
            parts.append(_turn_src("actors[%d].turn" % v[0], v[1], v[2]))
        else:  # command_actor
            parts.append("actors[%d].uses_command(%d)" % v)
    return " and ".join(parts) if parts else None


def troop_page_kwargs_src(c: dict[str, tuple[int, ...]]) -> list[tuple[str, Any]]:
    """Keyword arguments of a troop page's @page(...): its conditions, as `when=`."""
    second_switch_only = "switch_b" in c and "switch_a" not in c  # editor's 2nd switch slot, 1st unused
    when = troop_condition_src({k: v for k, v in c.items() if k != "switch_b"} if second_switch_only else c)
    kw: list[tuple[str, Any]] = []
    if when is not None:
        kw.append(("when", Raw(src=when)))
    if second_switch_only:
        kw.append(("switch_b", c["switch_b"][0]))
    return kw


_TROOP_WHEN = (
    "a troop page can require: up to two switches[id], variables[id] >= value, turn(n, every=m), "
    "fatigue(min, max), enemies[i].hp_percent(min, max), actors[id].hp_percent(min, max) and "
    "(2003) enemies[i].turn(n, every=m), actors[id].turn(n, every=m), actors[id].uses_command(n), "
    "joined with 'and'"
)


def _troop_condition_part(p) -> tuple[str, tuple[int, ...]]:
    if P.int_index(p, "switches") is not None:
        return "switch_a", (p.index,)
    if isinstance(p, P.Cmp) and P.var_index(p.left) is not None and P.is_int(p.right):
        if p.op != ">=":
            raise CompileError("troop pages only test variables[id] >= value", p.node)
        return "variable", (p.left.index, p.right)
    if isinstance(p, Call):
        enemy = P.int_index(p.target, "enemies") if p.target is not None else None
        actor = P.int_index(p.target, "actors") if p.target is not None else None
        if p.name == "turn" and (p.target is None or enemy is not None or actor is not None):
            a = K.Args(p, ["start", "every"])
            turns = (a.int("start"), a.int("every", 0))
            if p.target is None:
                return "turn", turns
            return ("turn_enemy", (enemy, *turns)) if enemy is not None else ("turn_actor", (actor, *turns))
        if p.name == "fatigue" and p.target is None:
            a = K.Args(p, ["min", "max"])
            return "fatigue", (a.int("min", 0), a.int("max", 100))
        if p.name == "hp_percent" and (enemy is not None or actor is not None):
            a = K.Args(p, ["min", "max"])
            hp = (a.int("min", 0), a.int("max", 100))
            return ("enemy_hp", (enemy, *hp)) if enemy is not None else ("actor_hp", (actor, *hp))
        if p.name == "uses_command" and actor is not None:
            return "command_actor", (actor, K.Args(p, ["command"]).int("command"))
    raise CompileError("unsupported troop page condition %s: %s" % (P.show(p), _TROOP_WHEN), getattr(p, "node", None))


def troop_condition_val(v, ctx: Ctx) -> dict[str, tuple[int, ...]]:
    """The conditions of `when=` (see troop_condition_src)."""
    out: dict[str, tuple[int, ...]] = {}
    for p in v.values if isinstance(v, P.And) else [v]:
        key, values = _troop_condition_part(p)
        if key == "switch_a" and key in out:
            key = "switch_b"
        if key in out:
            raise CompileError("a troop page can only have one %s condition" % key.replace("_", " "), p.node)
        for x in values:
            if not -0x80000000 <= x <= 0x7FFFFFFF:
                raise CompileError("%d is out of range (32 bit)" % x, getattr(p, "node", None))
        out[key] = values
    return out


def troop_page_props_from_call(call: Call, ctx: Ctx) -> tuple[dict[str, Any], int | None, bool]:
    """-> (props, explicit page id, raw flag) of a troop page's @page(...)."""
    allowed = ("id", "raw", "when", "switch_b")
    if call.args:
        raise CompileError("page() only takes keyword arguments", call.node)
    for k in call.kwargs:
        if k not in allowed:
            raise CompileError(
                "a troop page() takes when=, switch_b=, id= and raw= (got %r; sprites, triggers... are for map "
                "events)" % k,
                call.node,
            )
    a = K.Args(call, allowed)
    cond = troop_condition_val(a.get("when"), ctx) if a.get("when") is not None else {}
    if a.get("switch_b") is not None:
        if "switch_b" in cond:
            raise CompileError("page(): a page has two switch slots; when= already uses both", call.node)
        cond["switch_b"] = (a.int("switch_b"),)
    pid = a.get("id")
    if pid is not None and (not P.is_int(pid) or pid < 1):
        raise CompileError("page(): id must be a positive integer", call.node)
    return {"condition": cond}, pid, a.bool("raw", False)


def _troop_names(db: Struct, ctx: Ctx) -> dict[int, str]:
    """id -> editor name of the troops that have battle events."""
    return {
        item.id: ctx.dec(item.struct.get("name"))
        for item in db.get("troops")
        if not _unused_troop(troop_pages(item.struct))
    }


def troop_function_names(db: Struct, ctx: Ctx, keep: dict[int, str] | None = None) -> dict[int, str]:
    """Function names of the troops with battle events: the ones in the file (`keep`), else
    derived from the troop names, else troop_<id>."""
    from . import dsl

    return common_event_names(_troop_names(db, ctx), keep, "troop", dsl.TroopEventTable)


def decompile_troop_events(db: Struct, ctx: Ctx, target: str, keep: dict[int, str] | None = None) -> str:
    """database/troop_events.py.  keep: the function names of the current file."""
    with P.db_names(ctx):
        entries = _decompile_troop_entries(db, ctx, None, keep)
    out = [
        TROOP_HEADER,
        "",
        HEADER_NOTE.format(target=target),
        DSL_IMPORT,
        "",
        "",
        "class TroopEvents(TroopEventTable):",
    ]
    for n, lines in enumerate(entries.values()):
        if n:
            out.append("")
        out.extend(lines)
    if not entries:
        out.append(INDENT + "pass")
    out += ["", "", "troop_events = TroopEvents()"]
    return "\n".join(out).rstrip() + "\n"


def decompile_troop_entries(
    db: Struct, ctx: Ctx, ids: set[int] | None = None, keep: dict[int, str] | None = None
) -> dict[int, list[str]]:
    """id -> script lines of the troops with battle events (only of `ids` when given), in order."""
    with P.db_names(ctx):
        return _decompile_troop_entries(db, ctx, ids, keep)


def _decompile_troop_entries(
    db: Struct, ctx: Ctx, ids: set[int] | None = None, keep: dict[int, str] | None = None
) -> dict[int, list[str]]:
    names = troop_function_names(db, ctx, keep)
    out = {}
    for item in db.get("troops"):
        if ids is not None and item.id not in ids:
            continue
        pages = troop_pages(item.struct)
        if _unused_troop(pages):
            continue  # no battle events; the script only lists troops that have some
        lines = [INDENT + "@" + render_call("troop", [item.id, ctx.dec(item.struct.get("name"))])]
        lines.append(INDENT + "def %s():" % names[item.id])
        pad = INDENT * 2
        for n, pg in enumerate(pages, 1):
            kw = troop_page_kwargs_src(troop_condition_from_struct(pg.struct.get("condition")))
            if pg.id != n:
                kw.insert(0, ("id", pg.id))
            body, structured = body_lines(list(pg.struct.get("event_commands")), ctx, 3)
            if not structured:
                kw.append(("raw", True))
            if n > 1:
                lines.append("")
            lines.append(pad + "@" + render_call("page", [], kw))
            lines.append(pad + "def page_%d():" % n)
            lines.extend(_body(body, pad + INDENT))
        if not pages:
            lines.append(pad + "pass")
        out[item.id] = lines
        item.struct.release()  # one troop's pages at a time
    return out


def compile_troop_events_source(src: str, ctx: Ctx, filename: str = "<script>") -> list[TroopSpec]:
    with P.db_names(ctx):
        try:
            tree, entry = parse_by_entries(src, filename, "troop", INDENT)
            return _compile_troop_statements(tree, entry, src, ctx)
        except SyntaxError as e:
            raise CompileError("syntax error: %s" % e.msg, e) from None


def _compile_troop_statements(tree: ast.Module, entry: Callable, src: str, ctx: Ctx) -> list[TroopSpec]:
    ctx.comments = CommentIndex(src)
    classes = [st for st in tree.body if isinstance(st, ast.ClassDef)]
    if len(classes) != 1:
        raise CompileError("troop_events.py holds one class, TroopEvents", classes[1] if classes else None)
    for stmt in tree.body:
        if _is_header_stmt(stmt) or stmt is classes[0]:
            continue
        v = stmt.value if isinstance(stmt, ast.Assign) else None
        if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == classes[0].name):
            raise CompileError(
                "top level may only contain `class TroopEvents(...)` and `troop_events = TroopEvents()`", stmt
            )
    out: list[TroopSpec] = []
    seen: dict[int, int] = {}
    for stmt in (s for placeholder in classes[0].body for s in entry(placeholder) if not isinstance(s, ast.Pass)):
        if _is_header_stmt(stmt):
            continue
        if not isinstance(stmt, ast.FunctionDef):
            raise CompileError("TroopEvents may only contain @troop functions", stmt)
        dec = _decorator(stmt, "troop")
        a = K.Args(dec, ["id", "name"])
        tid = a.get("id")
        if not P.is_int(tid) or tid < 1:
            raise CompileError("@troop(...) needs the troop id, a positive integer (its slot in troops.py)", stmt)
        if tid in seen:
            raise CompileError("troop %d is listed twice (also on line %d)" % (tid, seen[tid]), stmt)
        seen[tid] = stmt.lineno
        a.str("name", "")  # a label: the name is changed in troops.py
        label, label_at = _label(dec)
        pages = []
        for sub in stmt.body:
            if isinstance(sub, ast.Pass) or _is_docstring(sub):
                continue
            if not isinstance(sub, ast.FunctionDef):
                raise CompileError("a @troop function may only contain @page functions", sub)
            props, pid, raw = troop_page_props_from_call(_decorator(sub, "page"), ctx)
            if raw:
                cmds = compile_raw_statements(sub.body, ctx)
            else:
                cmds = compile_statements(sub.body, ctx, 0, sub.lineno)
            pages.append(PageSpec(props=props, commands=cmds, id=pid, lineno=sub.lineno))
        out.append(TroopSpec(id=tid, pages=pages, lineno=stmt.lineno, label=label, label_at=label_at))
    return out


def _troop_spec(tid: int, pages: list[ArrayItem]) -> TroopSpec:
    return TroopSpec(
        id=tid,
        pages=[
            PageSpec(
                props={"condition": troop_condition_from_struct(pg.struct.get("condition"))},
                commands=list(pg.struct.get("event_commands")),
                id=pg.id if pg.id != n else None,
            )
            for n, pg in enumerate(pages, 1)
        ],
    )


def troop_event_specs(db: Struct, ids: set[int] | None = None) -> list[TroopSpec]:
    """Specs straight from the database (equal to compiling the decompiled text): of the
    troops that have battle events, or of `ids` when given."""
    out = []
    for item in db.get("troops"):
        if ids is not None and item.id not in ids:
            continue
        pages = troop_pages(item.struct)
        if ids is None and _unused_troop(pages):
            continue
        out.append(_troop_spec(item.id, pages))
    return out


def new_troop_page_struct(engine: str) -> Struct:
    """An empty battle event page laid out like the RPG Maker editor writes one."""
    p = Struct("TroopPage")
    cond = Struct("TroopPageCondition")
    cond.set("flags", IntVector([0] * (2 if engine == "2k3" else 1)))
    p.set("condition", cond)
    p.set("event_commands", CommandList([]))
    return p


def apply_troop_condition(p: Struct, c: dict[str, tuple[int, ...]], engine: str) -> None:
    """Patch a page's condition so it checks `c`, touching only what differs: the values
    of unchecked conditions and unknown flag bits stay."""
    cond = p.get("condition") if p.has("condition") else Struct("TroopPageCondition")
    old = cond.get("flags")
    flags = list(old) or [0] * (2 if engine == "2k3" else 1)
    need = max((TROOP_CONDITIONS[k][0] // 8 + 1 for k in c), default=0)
    flags += [0] * (need - len(flags))
    for key, (bit, _) in TROOP_CONDITIONS.items():
        if key in c:
            flags[bit // 8] |= 1 << (bit % 8)
        elif bit // 8 < len(flags):
            flags[bit // 8] &= ~(1 << (bit % 8)) & 0xFF
    if flags != list(old) or not cond.has("flags"):
        cond.set("flags", IntVector(flags, getattr(old, "trailer", b"")))
    for key, values in c.items():
        for field_name, value in zip(TROOP_CONDITIONS[key][1], values):
            if cond.get(field_name) != value:
                cond.set(field_name, value)
    p.set("condition", cond)


def _patch_troop_page(p: Struct, ps: PageSpec, engine: str) -> None:
    if troop_condition_from_struct(p.get("condition")) != ps.props["condition"]:
        apply_troop_condition(p, ps.props["condition"], engine)
    if list(p.get("event_commands")) != ps.commands:
        p.set("event_commands", CommandList(ps.commands))


def _set_troop_pages(st: Struct, pages: list[ArrayItem]) -> bool:
    """Write the pages into the troop's raw chunk if that changes its bytes."""
    data = write_array(pages)
    old = st.get("pages", None)
    if data == old or (old is None and not pages):
        return False
    st.set("pages", data)
    return True


def _patch_troop(st: Struct, spec: TroopSpec, engine: str) -> bool:
    pages = troop_pages(st)
    new_pages = []
    for n, ps in enumerate(spec.pages, 1):
        pid = ps.id if ps.id is not None else n
        if n - 1 < len(pages):
            item = pages[n - 1]
            if item.id != pid:
                item = ArrayItem(id=pid, struct=item.struct)
        else:
            item = ArrayItem(id=pid, struct=new_troop_page_struct(engine))
        _patch_troop_page(item.struct, ps, engine)
        new_pages.append(item)
    return _set_troop_pages(st, new_pages)


def apply_troop_events(db: Struct, specs: list, engine: str) -> dict[str, list[int]]:
    """Patch the battle events of the database's troops to match specs.

    Only the `pages` chunk of the troops whose pages change is written: names,
    monsters... (database/troops.py) stay as they are.  A troop missing from the
    script gets the one empty page of a new troop; a troop must exist in the
    database (troops.py adds troops)."""
    items = db.get("troops")
    current = {it.id: it for it in items}
    by_id: dict[int, Any] = {}
    for s in specs:
        if s.id not in current:
            raise ValueError("troop %d is not in the database: add it to database/troops.py first" % s.id)
        by_id[s.id] = s
    summary: dict[str, list[int]] = {"added": [], "removed": [], "changed": []}
    for item in items:
        spec = by_id.get(item.id)
        if isinstance(spec, KeepSpec):
            continue  # unchanged since the last sync: left as it is
        unused = _unused_troop(troop_pages(item.struct))
        if spec is None:
            if not unused and _set_troop_pages(item.struct, [ArrayItem(id=1, struct=new_troop_page_struct(engine))]):
                summary["removed"].append(item.id)
        elif _patch_troop(item.struct, spec, engine):
            summary["added" if unused else "changed"].append(item.id)
    return summary
