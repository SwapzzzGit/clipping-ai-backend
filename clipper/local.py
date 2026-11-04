"""Local video files uploaded from the PC: metadata, speech-to-text, and cutting clips.

An uploaded video has no YouTube captions, so its transcript comes from Whisper. Clip ranges
are cut straight from the file with ffmpeg instead of being downloaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2

from clipper.util import ClipperError, has_audio, log, read_json, write_json

UPLOAD_PREFIX = "upload:"
UPLOAD_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
META_SUFFIX = ".meta.json"
DEFAULT_TRANSCRIBE = {"model": "small", "device": "cpu", "compute_type": "int8"}

_whisper = None
_whisper_key = None


def is_upload(url: str) -> bool:
    return url.startswith(UPLOAD_PREFIX)


def upload_id_from_url(url: str) -> str:
    return url[len(UPLOAD_PREFIX):]


def find_upload(uploads_dir: Path, upload_id: str) -> Path | None:
    """The uploaded video file for an id (its sidecar metadata file is ignored)."""
    if not upload_id.isalnum():
        return None
    for f in sorted(uploads_dir.glob(f"{upload_id}.*")):
        if not f.name.endswith(META_SUFFIX) and f.suffix.lower() in UPLOAD_EXTENSIONS:
            return f
    return None


def upload_name(uploads_dir: Path, upload_id: str) -> str:
    meta = uploads_dir / f"{upload_id}{META_SUFFIX}"
    return read_json(meta).get("name", upload_id) if meta.is_file() else upload_id


def load_upload_info(uploads_dir: Path, work_root: Path, upload_id: str) -> tuple[dict, Path]:
    """Metadata in the same shape as a YouTube info dict, cached as work/<id>/info.json."""
    work = work_root / upload_id
    cached = work / "info.json"
    if cached.is_file():
        return read_json(cached), work
    src = find_upload(uploads_dir, upload_id)
    if not src:
        raise ClipperError("The uploaded video is missing. Upload it again.")
    cap = cv2.VideoCapture(str(src))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        opened = cap.isOpened()
    finally:
        cap.release()
    duration = frames / fps if fps else 0
    if not opened or duration <= 0:
        raise ClipperError("Could not read the uploaded video. Try an MP4 (H.264) file.")
    name = upload_name(uploads_dir, upload_id)
    info = {
        "id": upload_id,
        "title": Path(name).stem,
        "channel": None,
        "duration": round(duration, 3),
        "thumbnail": None,
        "local": True,
        "source_name": name,
    }
    write_json(cached, info)
    return info, work


def _model(cfg: dict):
    """One Whisper model per process, reloaded only when the settings change."""
    global _whisper, _whisper_key
    key = (cfg["model"], cfg["device"], cfg["compute_type"])
    if _whisper is None or _whisper_key != key:
        from faster_whisper import WhisperModel

        log.info("loading Whisper %s (%s, %s)", cfg["model"], cfg["device"], cfg["compute_type"])
        try:
            _whisper = WhisperModel(cfg["model"], device=cfg["device"], compute_type=cfg["compute_type"])
        except (RuntimeError, OSError) as e:
            if cfg["device"] == "cpu":
                raise
            # e.g. CUDA requested but its libraries are missing: run on the CPU instead
            log.warning("Whisper on %s failed (%s); using the CPU", cfg["device"], e)
            cfg = {**cfg, "device": "cpu"}
            _whisper = WhisperModel(cfg["model"], device="cpu", compute_type=cfg["compute_type"])
        _whisper_key = (cfg["model"], cfg["device"], cfg["compute_type"])
    return _whisper


def get_local_transcript(info: dict, work: Path, src: Path, lang: str, transcribe: dict | None = None,
                         ffmpeg: str | None = None) -> dict:
    """Speech-to-text with word timings, in the same shape as clipper.transcript.get_transcript."""
    cache = work / "transcript.json"
    if cache.is_file():
        cached = read_json(cache)
        if cached.get("requested_lang") == lang and cached.get("source") in ("whisper", "none"):
            log.info("transcript cached (whisper, %d segments)", len(cached["segments"]))
            return cached

    if ffmpeg is not None and not has_audio(ffmpeg, src):
        # A picture-only file has nothing to transcribe (Whisper cannot read a missing audio track).
        log.info("no audio track: nothing to transcribe")
        result = {"source": "none", "lang": None, "requested_lang": lang, "has_word_timings": False,
                  "segments": [], "heatmap": []}
        write_json(cache, result)
        return result

    cfg = {**DEFAULT_TRANSCRIBE, **(transcribe or {})}
    model = _model(cfg)
    log.info("transcribing the uploaded audio (%s)", cfg["model"])
    segs, meta = model.transcribe(str(src), language=lang or None, word_timestamps=True, vad_filter=True)
    segments = []
    for seg in segs:
        text = seg.text.strip()
        if not text:
            continue
        words = [
            {"start": round(w.start, 3), "end": round(max(w.end, w.start + 0.05), 3), "text": w.word.strip()}
            for w in (seg.words or []) if w.word.strip()
        ]
        segments.append({"start_sec": round(seg.start, 3), "end_sec": round(seg.end, 3), "text": text, "words": words})
        if len(segments) % 100 == 0:
            log.info("transcribed up to %s of %s", _mmss(seg.end), _mmss(info.get("duration") or 0))
    if not segments:
        log.info("no speech found: clips will be picked from the picture and the sound")

    result = {
        "source": "whisper" if segments else "none",
        "lang": meta.language,
        "requested_lang": lang,
        "has_word_timings": any(s["words"] for s in segments),
        "segments": segments,
        "heatmap": [],
    }
    write_json(cache, result)
    log.info("transcript: %d segments, word timings=%s", len(segments), result["has_word_timings"])
    return result


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def cut_upload_clip(src: Path, clip: dict, raw_dir: Path, cfg: dict, ffmpeg: str, duration: float) -> dict:
    """Cut [start-pad, end+pad] of one clip from the uploaded file, re-encoded so cuts are exact."""
    from clipper.download import is_valid_video

    d = cfg["download"]
    start = max(0.0, clip["start_sec"] - d["padding_sec"])
    end = min(duration or float("inf"), clip["end_sec"] + d["padding_sec"])
    stem = f"clip_{clip['id']:02d}_{int(start * 1000)}_{int(end * 1000)}"
    out = raw_dir / f"{stem}.mp4"
    result = {"path": str(out), "offset_sec": start, "end_sec": end}
    if is_valid_video(out, ffmpeg):
        log.info("clip %d: cut cached", clip["id"])
        return result

    length = f"{end - start:.3f}"
    cmd = [ffmpeg, "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if has_audio(ffmpeg, src):
        cmd += ["-t", length]
    else:  # picture only: add silence so the render still has an audio track
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", length, "-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    log.info("clip %d: cutting %.1f-%.1f s from the upload", clip["id"], start, end)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode == 0 and is_valid_video(out, ffmpeg):
        return result
    error = (proc.stderr or "").strip()[-500:] or "could not cut this clip from the upload"
    log.warning("clip %d: cut failed: %s", clip["id"], error)
    return {**result, "error": error}
