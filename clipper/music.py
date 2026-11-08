"""Background-music library: a folder of tracks, each matched to a clip by its mood.

Audio files go in assets/music/ (see the README there). An optional tracks.json in that
folder tags each file, e.g. {"calm-piano.mp3": {"name": "Calm piano", "moods": ["calm"],
"instrumental": true}}. A file without an entry counts as calm, with vocals unknown.
"""

from __future__ import annotations

from pathlib import Path

from clipper.util import ClipperError, log, read_json

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "assets" / "music"
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
MOODS = ("calm", "upbeat", "tense", "emotional", "serious", "funny")
DEFAULT_MOOD = "calm"


def clean_mood(value) -> str:
    """A mood the library knows, else calm: a model may invent a mood or leave the field out."""
    return value if value in MOODS else DEFAULT_MOOD


def list_tracks(folder: Path = DEFAULT_DIR) -> list[dict]:
    if not folder.is_dir():
        return []
    tags = read_json(folder / "tracks.json") if (folder / "tracks.json").is_file() else {}
    tracks = []
    for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
            continue
        meta = tags.get(p.name, {})
        moods = [m for m in meta.get("moods", [DEFAULT_MOOD]) if m in MOODS] or [DEFAULT_MOOD]
        tracks.append({
            "file": p.name,
            "name": meta.get("name") or p.stem.replace("_", " ").replace("-", " "),
            "moods": moods,
            "instrumental": bool(meta.get("instrumental", False)),
        })
    return tracks


def resolve(name: str, folder: Path = DEFAULT_DIR) -> Path:
    """The library file called `name`. Only a bare file name is trusted, never a path."""
    path = folder / Path(name or "").name
    if not name or path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
        raise ClipperError(f"Music track {name!r} is not in the library.")
    return path


def pick_track(mood: str, index: int, folder: Path = DEFAULT_DIR) -> dict | None:
    """The track for a mood. `index` (the clip number) spreads clips over the matching tracks."""
    tracks = list_tracks(folder)
    matching = [t for t in tracks if mood in t["moods"]] or tracks  # no track for this mood: any track
    ranked = sorted(matching, key=lambda t: not t["instrumental"])  # instrumental first: vocals fight the speech
    return ranked[index % len(ranked)] if ranked else None


def clip_music(clip: dict, choice: str | None, volume: int, folder: Path = DEFAULT_DIR) -> dict | None:
    """What to mix under one clip. `choice` is None (no music), "auto" (match the clip's mood) or a file name."""
    if not choice:
        return None
    if choice == "auto":
        track = pick_track(clean_mood(clip.get("music_mood")), clip["id"], folder)
        if track is None:
            log.warning("clip %d: the music library is empty; no music", clip["id"])
            return None
        path = folder / track["file"]
    else:
        path = resolve(choice, folder)
    return {"path": str(path), "name": path.name, "volume": volume}
