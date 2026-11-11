"""Layout glyphs and small builders for screen texts (plain HTML).

A screen is a title line, an empty line, then blocks joined by an empty line.
A block is a header line plus ``Label: value`` lines indented by
``SUBITEM_INDENT`` (non-breaking spaces, so Telegram never collapses them).
Never indent with raw ASCII spaces; numbered steps sit flush left.

The builders do not escape their arguments: pass already-escaped HTML.
"""
from __future__ import annotations

import html
import re
from typing import Iterable, Sequence

NBSP = " "
SUBITEM_INDENT = NBSP * 7
BULLET = "•"
SEP = " · "

_TAG_RE = re.compile(r"<[^>]+>")
TOAST_MAX_LEN = 200


def bullet(text: str, indent: str = SUBITEM_INDENT) -> str:
    return f"{indent}{BULLET} {text}"


def sub_line(text: str) -> str:
    return f"{SUBITEM_INDENT}{text}"


def title(emoji: str, text: str, *facts: object) -> str:
    """``'👥 <b>Users</b> · 12'``: the one title line of a screen or push.

    Falsy facts (``None``, ``''``, ``0``) are left out.
    """
    head = f"{emoji} <b>{text}</b>" if emoji else f"<b>{text}</b>"
    return head + "".join(f"{SEP}{fact}" for fact in facts if fact)


def section(emoji: str, name: str, lines: Iterable[str | None]) -> list[str] | None:
    """A block: ``'<emoji> <b>name</b>'`` plus indented lines, or ``None`` when empty."""
    body = [sub_line(line) for line in lines if line]
    if not body:
        return None
    return [f"{emoji} <b>{name}</b>" if emoji else f"<b>{name}</b>", *body]


def join_blocks(*blocks: str | Sequence[str] | None) -> str:
    """Join blocks with an empty line; a block is a string or a list of lines.

    ``None`` and empty blocks are dropped, so a block that does not apply is
    simply left out.
    """
    parts: list[str] = []
    for block in blocks:
        if not block:
            continue
        if isinstance(block, str):
            text = block
        else:
            text = "\n".join(line for line in block if line)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def with_banner(banner: str | None, screen: str) -> str:
    """Put a one-line result banner above a redrawn screen."""
    return f"{banner}\n\n{screen}" if banner else screen


def toast_from_html(text: str | None) -> str:
    """The first non-empty line of an HTML text, as a plain callback toast (≤200 chars)."""
    for line in (text or "").splitlines():
        plain = html.unescape(_TAG_RE.sub("", line)).strip()
        if plain:
            return plain[:TOAST_MAX_LEN]
    return ""


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """A ``█``/``░`` bar: any progress shows a cell, full only when all are done."""
    if width <= 0:
        return ""
    if total <= 0:
        return "░" * width
    done = max(0, min(done, total))
    filled = round(done * width / total)
    if done > 0:
        filled = max(filled, 1)
    if done < total:
        filled = min(filled, width - 1)
    else:
        filled = width
    return "█" * filled + "░" * (width - filled)


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit of Telegram's 4096 limit."""
    return len(text.encode("utf-16-le")) // 2


def visible_len(html_text: str) -> int:
    """What Telegram counts against 4096: the text after entity parsing, in UTF-16 units.

    Tags do not count and ``&amp;`` is one character, so this is the length
    of the unescaped text without tags.
    """
    return utf16_len(html.unescape(_TAG_RE.sub("", html_text)))


def cut_visible(plain: str, room_utf16: int, ellipsis: str = "…") -> str:
    """Plain text cut to ``room_utf16`` UTF-16 units, ending in ``ellipsis`` when cut.

    Never splits a surrogate pair (an emoji counts as two units).
    """
    if utf16_len(plain) <= room_utf16:
        return plain
    room = room_utf16 - utf16_len(ellipsis)
    kept: list[str] = []
    used = 0
    for char in plain:
        width = utf16_len(char)
        if used + width > room:
            break
        kept.append(char)
        used += width
    return "".join(kept).rstrip() + ellipsis


_WRAPPED_RE = re.compile(r"^(<(\w+)(?:\s[^>]*)?>)([^<]*)(</\2>)$")


def _cut_line(line: str, room_utf16: int) -> str:
    """One HTML line cut to ``room_utf16`` visible units, still valid HTML.

    A line wrapped in one tag (``<code>…</code>``) keeps the tag; any other
    markup is dropped and the plain text is cut and escaped again.
    """
    match = _WRAPPED_RE.match(line)
    if match:
        opening, _, inner, closing = match.groups()
        plain = html.unescape(inner)
        return opening + html.escape(cut_visible(plain, room_utf16), quote=False) + closing
    plain = html.unescape(_TAG_RE.sub("", line))
    return html.escape(cut_visible(plain, room_utf16), quote=False)


def expandable(
    lines: Sequence[str],
    budget_utf16: int,
    more_noun: str,
    more_plural: str | None = None,
) -> str:
    """Fold ``lines`` into ``<blockquote expandable>`` within a visible UTF-16 budget.

    The budget is measured like Telegram does: visible text, tags not
    counted, ``&amp;`` as one character. Whole lines are kept while they fit;
    the rest is counted in a last line ``'…and N more <nouns>'``. When not
    even the first line fits, a cut copy of it (ending in '…') is kept, so a
    long comment or requisites line is never lost entirely. Empty input
    gives ``''``.
    """
    from app.ui.text import plural_word  # text.py imports this module's glyphs

    items = [line for line in lines if line]
    if not items:
        return ""
    opening, closing = "<blockquote expandable>", "</blockquote>"

    def render(kept: Sequence[str], rest: int) -> str:
        body = list(kept)
        if rest:
            body.append(f"…and {rest} more {plural_word(rest, more_noun, more_plural)}")
        return opening + "\n".join(body) + closing

    full = render(items, 0)
    if visible_len(full) <= budget_utf16:
        return full
    kept: list[str] = []
    for index, line in enumerate(items):
        rest = len(items) - index - 1
        candidate = render([*kept, line], rest)
        if visible_len(candidate) > budget_utf16:
            if not kept:
                # The first line alone is over budget: keep it cut, not dropped.
                room = budget_utf16 - visible_len(render([], rest)) - (1 if rest else 0)
                if room > 1:
                    kept.append(_cut_line(line, room))
            break
        kept.append(line)
    return render(kept, len(items) - len(kept))
