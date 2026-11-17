"""Text shown to the admin may come from an attacker: it must not render as markdown."""
from __future__ import annotations

import pytest

from custom_components.private_hacs.util import plain


def test_markdown_syntax_is_escaped():
    out = plain("[click me](https://evil.example) **bold** _it_ `code` <b>x</b> ![i](u)")
    for char in "[]*_`<>":
        assert f"\\{char}" in out
        assert out.replace(f"\\{char}", "").count(char) == 0
    assert "](" not in out.replace("\\]", "")  # no way left to build a link or image


def test_parentheses_are_left_alone():
    assert plain("Refusing to go from 1.1.0 down to 1.0.0 (use rollback)") == (
        "Refusing to go from 1.1.0 down to 1.0.0 (use rollback)"
    )


def test_control_characters_and_newlines_become_spaces():
    assert plain("a\nb\r\nc\x00d\x1b[31m") == "a b c d \\[31m"


def test_length_is_capped():
    out = plain("x" * 1000, limit=50)
    assert out == "x" * 50 + "…"


@pytest.mark.parametrize("value", [ValueError("a_b"), 5, None])
def test_accepts_any_object(value):
    assert isinstance(plain(value), str)


def test_plain_text_is_untouched():
    text = "acme/demo v1.2.3: signature is not valid: signed by a key that is not in the list"
    assert plain(text) == text
