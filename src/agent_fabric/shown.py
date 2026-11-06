"""Text from a note or a repo, made safe to show to a person or an agent (docs/design.md §7)."""

import unicodedata

# Control characters drive a terminal; format characters (bidi overrides, zero-width ones) change how text reads on
# screen without showing themselves; line and paragraph separators break a line in many viewers.
UNSEEN = frozenset({"Cc", "Cf", "Zl", "Zp"})


def shown(text: str, *, lines: bool = False) -> str:
    """`text` with every character that acts instead of showing written as an escape: `\\x1b`, `\\u202e`. A tab
    stays; with `lines`, so do line breaks, for text shown line by line."""
    return "".join(_escaped(c) if _unseen(c, lines) else c for c in text)


def _unseen(c: str, lines: bool) -> bool:
    return c != "\t" and not (lines and c == "\n") and unicodedata.category(c) in UNSEEN


def _escaped(c: str) -> str:
    code = ord(c)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
