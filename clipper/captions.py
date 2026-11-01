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


def group_words(words: list[dict], per_line: int) -> list[list[dict]]:
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (
            len(cur) >= per_line
            or nxt is None
            or SENTENCE_END.search(w["text"])
            or nxt.get("speaker_change")
            or nxt["start"] - w["end"] > GROUP_BREAK_PAUSE
        ):
            groups.append(cur)
            cur = []
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


def build_ass(words: list[dict], t0: float, duration: float, r: dict) -> str:
    """ASS script; `t0` is the source time that maps to 0 in the rendered clip."""
    w, h = r["width"], r["height"]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{r['caption_font']},{r['caption_font_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,80,80,{int(h * 0.27)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    hi = r["caption_highlight"]
    lines = []
    groups = group_words(words, r["caption_words_per_line"])
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else float("inf")
        group_end = min(group[-1]["end"] + 0.25, next_start, t0 + duration)
        for wi, word in enumerate(group):
            ev_start = word["start"]
            ev_end = group[wi + 1]["start"] if wi + 1 < len(group) else group_end
            if ev_end <= ev_start:
                continue
            text = " ".join(
                f"{{\\c{hi}}}{_esc(x['text'])}{{\\c&H00FFFFFF&}}" if j == wi else _esc(x["text"])
                for j, x in enumerate(group)
            )
            lines.append(f"Dialogue: 0,{_ts(ev_start - t0)},{_ts(ev_end - t0)},Default,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"


def write_ass(path: Path, segments: list[dict], start: float, end: float, t0: float, r: dict) -> int:
    words = clip_words(segments, start, end)
    path.write_text(build_ass(words, t0, end - t0, r), encoding="utf-8")
    return len(words)
