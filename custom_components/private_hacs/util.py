"""Small helpers."""
from __future__ import annotations

import re

# Enough to stop links, images, emphasis, code and HTML. Parentheses, `#` and `|` are harmless
# once `[` and newlines are gone, and escaping them would only litter normal messages.
_MARKDOWN = re.compile(r"([\\`*_\[\]<>])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def plain(text: object, limit: int = 300) -> str:
    """Make untrusted text safe to show in a Home Assistant dialog or notification.

    Descriptions and notifications are rendered as markdown: strip control characters,
    escape markdown syntax (no injected links or formatting) and cut the length.
    """
    cleaned = _CONTROL.sub(" ", str(text)).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return _MARKDOWN.sub(r"\\\1", cleaned)
