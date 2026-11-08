"""Burned-in, word-highlighted captions as an ASS subtitle file."""

from __future__ import annotations

import re
from pathlib import Path

from clipper.candidates import SENTENCE_END

GROUP_BREAK_PAUSE = 0.5


def clip_words(segments: list[dict], start: float, end: float) -> list[dict]:
    """Words inside [start, end]. Segments without word timings are spread evenly over their span."""
    words = []
    for s in segments:
        if s["end_sec"] <= start or s["start_sec"] >= end:
            continue
        if s["words"]:
            words += [w for w in s["words"] if start - 0.05 <= w["start"] < end]
            continue
        tokens = s["text"].split()
        span = (s["end_sec"] - s["start_sec"]) / max(1, len(tokens))
        for i, tok in enumerate(tokens):
            ws = s["start_sec"] + i * span
            if start - 0.05 <= ws < end:
                words.append({"start": ws, "end": ws + span, "text": tok})
    return words


def group_words(words: list[dict], per_line: int, max_chars: int = 0) -> list[list[dict]]:
    """Split the words into caption lines: at a sentence end, a long pause, a speaker change, `per_line`
    words, or (when set) `max_chars` characters. A single word longer than the limit still gets its own line."""
    groups, cur, chars = [], [], 0
    for i, w in enumerate(words):
        if cur and max_chars and chars + 1 + len(w["text"]) > max_chars:
            groups.append(cur)
            cur, chars = [], 0
        cur.append(w)
        chars += len(w["text"]) + (1 if chars else 0)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (
            len(cur) >= per_line
            or nxt is None
            or SENTENCE_END.search(w["text"])
            or nxt.get("speaker_change")
            or nxt["start"] - w["end"] > GROUP_BREAK_PAUSE
        ):
            groups.append(cur)
            cur, chars = [], 0
    return groups


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return re.sub(r"[{}\\]", "", text)


def build_ass(words: list[dict], t0: float, duration: float, r: dict, layout: str = "crop",
              split: list[tuple[float, float]] = ()) -> str:
    """ASS script; `t0` is the source time that maps to 0 in the rendered clip.

    "crop": captions sit in the lower part of the 9:16 frame. "fit": the picture is letterboxed,
    so captions go in the blurred band above it. Inside `split` spans (clip seconds) the frame is two
    stacked people, so captions go on the seam between them.
    """
    w, h = r["width"], r["height"]
    align, margin = (2, int(h * 0.27)) if layout == "crop" else (8, int(h * 0.12))
    bold = -1 if r.get("caption_bold", True) else 0
    shadow = r.get("caption_shadow", 3)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{r['caption_font']},{r['caption_font_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,{bold},0,0,0,100,100,0,0,1,7,{shadow},{align},80,80,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    hi = r["caption_highlight"]
    animation = r.get("caption_animation", "words")
    lines = []

    def dialogue(ev_start: float, ev_end: float, text: str) -> str:
        at = ev_start - t0
        if any(a <= at < b for a, b in split):
            text = f"{{\\an5\\pos({w // 2},{h // 2})}}{text}"   # centre of the frame, where the halves meet
        return f"Dialogue: 0,{_ts(ev_start - t0)},{_ts(ev_end - t0)},Default,,0,0,0,,{text}"

    groups = group_words(words, r["caption_words_per_line"], r.get("caption_max_chars", 0))
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else float("inf")
        group_end = min(group[-1]["end"] + 0.25, next_start, t0 + duration)
        if animation == "lines":
            # The whole line stays on screen, with no word highlighted.
            if group_end > group[0]["start"]:
                lines.append(dialogue(group[0]["start"], group_end, " ".join(_esc(x["text"]) for x in group)))
            continue
        for wi, word in enumerate(group):
            ev_start = word["start"]
            ev_end = group[wi + 1]["start"] if wi + 1 < len(group) else group_end
            if ev_end <= ev_start:
                continue
            text = " ".join(
                f"{{\\c{hi}}}{_esc(x['text'])}{{\\c&H00FFFFFF&}}" if j == wi else _esc(x["text"])
                for j, x in enumerate(group)
            )
            lines.append(dialogue(ev_start, ev_end, text))
    return header + "\n".join(lines) + "\n"


def word_key(start: float) -> str:
    """How a reviewer's caption edit finds its word: the word's start in source seconds, to the millisecond.
    The start does not change when the clip is trimmed or cut, so an edit stays with its word."""
    return f"{start:.3f}"


def apply_caption_edits(words: list[dict], edits: dict | None) -> list[dict]:
    """The words with the text a reviewer corrected. A word corrected to nothing is left out of the captions."""
    if not edits:
        return words
    out = []
    for w in words:
        key = word_key(w["start"])
        if key not in edits:
            out.append(w)
        elif edits[key]:
            out.append({**w, "text": edits[key]})
    return out


def write_ass(path: Path, segments: list[dict], start: float, end: float, t0: float, r: dict,
              skip: list[tuple[float, float]] = (), layout: str = "crop", split: list[tuple[float, float]] = (),
              edits: dict | None = None) -> int:
    """Write the caption file; words spoken inside a `skip` span (source time) get no caption.
    `edits` replaces the text of the words a reviewer corrected (see apply_caption_edits)."""
    words = apply_caption_edits(clip_words(segments, start, end), edits)
    words = [w for w in words if not any(a <= w["start"] < b for a, b in skip)]
    path.write_text(build_ass(words, t0, end - t0, r, layout, split), encoding="utf-8")
    return len(words)
