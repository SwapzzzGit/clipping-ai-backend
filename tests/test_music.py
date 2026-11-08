import json

import pytest

from clipper import candidates as cand
from clipper.music import MOODS, clean_mood, clip_music, list_tracks, pick_track, resolve
from clipper.util import ClipperError


@pytest.fixture
def library(tmp_path):
    (tmp_path / "calm-piano.mp3").write_bytes(b"x")
    (tmp_path / "loud-beat.wav").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not audio", encoding="utf-8")
    (tmp_path / "tracks.json").write_text(json.dumps({
        "calm-piano.mp3": {"name": "Calm piano", "moods": ["calm", "serious"], "instrumental": True},
        "loud-beat.wav": {"moods": ["upbeat"], "instrumental": False},
    }), encoding="utf-8")
    return tmp_path


def test_library_lists_audio_files_with_their_tags(library):
    tracks = {t["file"]: t for t in list_tracks(library)}
    assert set(tracks) == {"calm-piano.mp3", "loud-beat.wav"}  # the .txt is not a track
    assert tracks["calm-piano.mp3"]["name"] == "Calm piano" and tracks["calm-piano.mp3"]["instrumental"] is True
    assert tracks["loud-beat.wav"]["moods"] == ["upbeat"] and tracks["loud-beat.wav"]["name"] == "loud beat"


def test_missing_library_folder_has_no_tracks(tmp_path):
    assert list_tracks(tmp_path / "nope") == []


def test_only_a_file_name_in_the_library_is_trusted(library):
    assert resolve("calm-piano.mp3", library) == library / "calm-piano.mp3"
    assert resolve("../calm-piano.mp3", library).parent == library  # the folder part is dropped
    for bad in ["missing.mp3", "notes.txt", ""]:
        with pytest.raises(ClipperError):
            resolve(bad, library)


def test_auto_picks_a_matching_track_instrumental_first(library):
    assert pick_track("calm", 0, library)["file"] == "calm-piano.mp3"
    assert pick_track("upbeat", 0, library)["file"] == "loud-beat.wav"
    assert pick_track("funny", 0, library)["file"] in {"calm-piano.mp3", "loud-beat.wav"}  # no match: any track
    assert pick_track("calm", 0, library / "empty") is None


def test_clips_with_the_same_mood_spread_over_the_matching_tracks(library):
    (library / "calm-two.mp3").write_bytes(b"x")  # untagged, so calm but not instrumental
    picks = [pick_track("calm", i, library)["file"] for i in (0, 1, 2)]
    assert picks == ["calm-piano.mp3", "calm-two.mp3", "calm-piano.mp3"]


def test_clip_music_none_auto_or_a_named_track(library):
    clip = {"id": 2, "music_mood": "upbeat"}
    assert clip_music(clip, None, 20, library) is None
    auto = clip_music(clip, "auto", 30, library)
    assert auto["name"] == "loud-beat.wav" and auto["volume"] == 30
    assert clip_music({"id": 1, "music_mood": "bogus"}, "auto", 20, library)["name"] == "calm-piano.mp3"
    assert clip_music(clip, "calm-piano.mp3", 10, library)["name"] == "calm-piano.mp3"
    with pytest.raises(ClipperError):
        clip_music(clip, "nope.mp3", 10, library)


def test_unknown_moods_become_calm():
    assert clean_mood("tense") == "tense" and clean_mood("dance") == "calm" and clean_mood(None) == "calm"
    assert set(MOODS) >= {"calm", "upbeat", "tense", "emotional", "serious", "funny"}


def test_picks_from_the_model_carry_a_valid_mood(cfg, long_segments):
    transcript = {"segments": long_segments, "heatmap": []}
    picks = [{"start_sec": 0.0, "end_sec": 40.0, "music_mood": "tense"},
             {"start_sec": 60.0, "end_sec": 100.0, "music_mood": "dance"}]
    clips = cand.normalize_external_picks(picks, transcript, cfg)
    moods = [c["music_mood"] for c in clips]
    assert "tense" in moods and set(moods) <= set(MOODS)
