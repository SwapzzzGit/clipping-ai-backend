"""Step 1: fetch metadata + timed transcript without downloading any video."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import yt_dlp

from clipper.util import ClipperError, js_runtime, log, read_json, write_json

NOISE = re.compile(r"\[(?:music|applause|laughter|inaudible|__)\]|♪+", re.I)


def fetch_info(url: str, work_root: Path) -> tuple[dict, Path]:
    """Extract video metadata (no download). Cached as work/<id>/info.json."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if js_runtime():
        opts["js_runtimes"] = {js_runtime(): {}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise ClipperError(f"Could not read video info: {e}")
        info = ydl.sanitize_info(info)
    work = work_root / info["id"]
    write_json(work / "info.json", info)
    return info, work


def load_or_fetch_info(url: str, work_root: Path) -> tuple[dict, Path]:
    vid = video_id_from_url(url)
    if vid and (work_root / vid / "info.json").is_file():
        log.info("info.json cached")
        return read_json(work_root / vid / "info.json"), work_root / vid
    return fetch_info(url, work_root)


def video_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", url)
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[\w-]{11}", url) else None


def pick_subtitle_track(info: dict, lang: str) -> tuple[str, str, str]:
    """Return (source, lang_key, json3_url). Manual subs beat auto captions.

    For auto captions YouTube lists every machine-translation target too, so prefer
    "<lang>-orig" (the original-language ASR track) over a plain "<lang>" translation.
    """

    def json3(tracks: list[dict]) -> str | None:
        return next((t["url"] for t in tracks if t.get("ext") == "json3"), None)

    def candidates(keys, preferred):
        exact = [k for k in preferred if k in keys]
        prefix = sorted(k for k in keys if k.split("-")[0] == lang and k not in exact)
        return exact + prefix

    manual = info.get("subtitles") or {}
    for key in candidates(manual.keys(), [lang]):
        if key == "live_chat":
            continue
        url = json3(manual[key])
        if url:
            return "manual", key, url

    auto = info.get("automatic_captions") or {}
    for key in candidates(auto.keys(), [f"{lang}-orig", lang]):
        url = json3(auto[key])
        if url:
            return "auto", key, url

    available = sorted(set(manual) | {k for k in auto if k.endswith("-orig")})
    # TODO(whisper): transcribe audio-only download with Whisper when no captions exist.
    raise ClipperError(
        f"No '{lang}' captions found for this video (available: {', '.join(available) or 'none'}). "
        "Try --lang <code>. Whisper transcription is not supported yet."
    )


def parse_json3(data: dict) -> list[dict]:
    """Normalize YouTube json3 captions into [{start_sec, end_sec, text, words}].

    Auto captions carry per-word offsets (tOffsetMs) and their events overlap in a
    rolling display, so each segment is clipped to start no later than the next one.
    `words` is [{start, end, text}] when timings exist, else [].
    """
    events = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        start = ev["tStartMs"] / 1000
        dur = ev.get("dDurationMs", 0) / 1000
        pieces = [(s.get("tOffsetMs", 0) / 1000, s.get("utf8", "")) for s in segs]
        text = _clean("".join(p for _, p in pieces))
        if not text:
            continue
        events.append({"start": start, "end": start + dur, "pieces": pieces, "timed": len(segs) > 1})

    out = []
    for i, ev in enumerate(events):
        next_start = events[i + 1]["start"] if i + 1 < len(events) else ev["end"]
        end = min(ev["end"], next_start) if next_start > ev["start"] else ev["end"]
        words = []
        if ev["timed"]:
            speaker_change = False
            for j, (off, raw) in enumerate(ev["pieces"]):
                w = _clean(raw)
                # ">>" marks a new speaker in YouTube ASR: keep it as a flag, not a word.
                if w.startswith(">>"):
                    speaker_change, w = True, w[2:].strip()
                if not w:
                    continue
                w_start = ev["start"] + off
                w_end = ev["start"] + ev["pieces"][j + 1][0] if j + 1 < len(ev["pieces"]) else end
                word = {"start": round(w_start, 3), "end": round(max(w_end, w_start + 0.05), 3), "text": w}
                if speaker_change:
                    word["speaker_change"], speaker_change = True, False
                words.append(word)
            if words:
                end = max(end, words[-1]["start"] + 0.05)
        text = _clean(" ".join(raw for _, raw in ev["pieces"])) if ev["timed"] else _clean(
            "".join(raw for _, raw in ev["pieces"])
        )
        out.append({"start_sec": round(ev["start"], 3), "end_sec": round(end, 3), "text": text, "words": words})
    return out


def _clean(text: str) -> str:
    text = html.unescape(text).replace("\n", " ")
    text = NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def heatmap_from_info(info: dict) -> list[dict]:
    return [
        {"start_sec": h["start_time"], "end_sec": h["end_time"], "value": h["value"]}
        for h in (info.get("heatmap") or [])
        if {"start_time", "end_time", "value"} <= h.keys()
    ]


def get_transcript(info: dict, work: Path, lang: str, force: bool = False) -> dict:
    """Return {source, lang, segments, heatmap}; cached as work/<id>/transcript.json."""
    cache = work / "transcript.json"
    if cache.is_file() and not force:
        cached = read_json(cache)
        if cached.get("requested_lang") == lang:
            log.info("transcript cached (%s captions, %d segments)", cached["source"], len(cached["segments"]))
            return cached

    source, key, url = pick_subtitle_track(info, lang)
    log.info("downloading %s captions (%s)", source, key)
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        raw = json.loads(ydl.urlopen(url).read().decode("utf-8"))
    write_json(work / f"subs.{key}.json3", raw)

    segments = parse_json3(raw)
    if not segments:
        raise ClipperError("Caption track is empty.")
    result = {
        "source": source,
        "lang": key,
        "requested_lang": lang,
        "has_word_timings": any(s["words"] for s in segments),
        "segments": segments,
        "heatmap": heatmap_from_info(info),
    }
    write_json(cache, result)
    log.info(
        "transcript: %d segments, word timings=%s, heatmap points=%d",
        len(segments), result["has_word_timings"], len(result["heatmap"]),
    )
    return result
