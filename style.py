"""
Small-caps Unicode text styling for user-facing bot messages.

Pure text transformation only - never touches database calls, callback
data, command names, or any logic. Safe to apply to display strings
anywhere without changing behavior.
"""
from __future__ import annotations

_SMALL_CAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "ꜱ", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}


def small_caps(text: str) -> str:
    """Converts a-z (case-insensitive) to small-caps Unicode. Numbers,
    punctuation, emoji, and existing HTML tags (<b>, <code>, etc.) pass
    through untouched, so this is safe to wrap around any plain label."""
    return "".join(_SMALL_CAPS_MAP.get(ch.lower(), ch) for ch in text)


def heading(text: str) -> str:
    """A bold small-caps heading, e.g. for section titles."""
    return f"<b>{small_caps(text)}</b>"


def label(text: str) -> str:
    """A bold small-caps inline label, e.g. 'Size:' before a value."""
    return f"<b>{small_caps(text)}</b>"
  
