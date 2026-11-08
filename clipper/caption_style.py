"""Per-clip caption settings. A reviewer can change one clip's captions without touching the job's.

A clip with no style of its own renders exactly as before: `caption_render` returns the job's render settings.
"""

from __future__ import annotations

from copy import deepcopy

# Fonts a person can pick. The renderer uses the ones installed on the computer, so these ship with Windows.
FONTS = ("Arial", "Arial Black", "Impact", "Verdana", "Tahoma", "Trebuchet MS", "Segoe UI", "Georgia")

# The colour of the word being spoken, as ASS colours (BGR order, as the caption renderer writes them).
HIGHLIGHTS = {
    "yellow": "&H0000E5FF",
    "white": "&H00FFFFFF",
    "cyan": "&H00FFE500",
    "green": "&H0000FF00",
    "orange": "&H00457AFF",
    "pink": "&H007F3DFF",
}

ANIMATIONS = ("words", "lines")

# What a clip gets when its style does not say otherwise. These match the caption renderer's original look.
DEFAULTS = {
    "enabled": True,
    "font": "Arial",
    "size": 84,
    "bold": True,
    "animation": "words",
    "max_chars": 0,
    "highlight": "yellow",
    "shadow": 3,
}


def caption_render(base: dict, style: dict | None) -> dict:
    """The render settings for one clip: the job's, with that clip's caption style laid over them."""
    r = deepcopy(base)
    r.setdefault("caption_bold", DEFAULTS["bold"])
    r.setdefault("caption_animation", DEFAULTS["animation"])
    r.setdefault("caption_max_chars", DEFAULTS["max_chars"])
    r.setdefault("caption_shadow", DEFAULTS["shadow"])
    if not style:
        return r
    s = {**DEFAULTS, **style}
    r["captions"] = bool(s["enabled"])
    r["caption_font"] = s["font"]
    r["caption_font_size"] = int(s["size"])
    r["caption_bold"] = bool(s["bold"])
    r["caption_animation"] = s["animation"]
    r["caption_max_chars"] = int(s["max_chars"])
    r["caption_highlight"] = HIGHLIGHTS[s["highlight"]]
    r["caption_shadow"] = int(s["shadow"])
    return r
