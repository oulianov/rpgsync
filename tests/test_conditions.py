"""Branch conditions: `and` is written as nested branches."""

import pytest

from rpgsync.commands import Ctx
from rpgsync.script import CompileError, compile_body_src

CTX = Ctx()


def _keys(src: str) -> list:
    return [c.key() for c in compile_body_src(src.split("\n"), CTX)]


@pytest.mark.parametrize(
    ("with_and", "nested"),
    [
        (
            'if switches[1] and variables[2] == 3:\n    text("A")',
            'if switches[1]:\n    if variables[2] == 3:\n        text("A")',
        ),
        (  # several and, grouped or not, with a comment in the body
            'if switches[1] and (variables[2] > 5 and not switches[3]):\n    # note\n    text("A")\ntext("B")',
            'if switches[1]:\n    if variables[2] > 5:\n        if not switches[3]:\n'
            '            # note\n            text("A")\ntext("B")',
        ),
        (  # in an elif
            'if switches[1]:\n    text("A")\nelif switches[2] and switches[3]:\n    text("B")',
            'if switches[1]:\n    text("A")\nelse:\n    if switches[2]:\n        if switches[3]:\n            text("B")',
        ),
    ],
)
def test_and_is_nested_branches(with_and, nested):
    assert _keys(with_and) == _keys(nested)


def test_and_with_else_is_an_error():
    with pytest.raises(CompileError, match="can't have an else"):
        compile_body_src(['if switches[1] and switches[2]:', '    text("A")', "else:", '    text("B")'], CTX)
