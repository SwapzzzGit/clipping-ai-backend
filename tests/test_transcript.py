import pytest

from clipper.transcript import heatmap_from_info, parse_json3, pick_subtitle_track, video_id_from_url
from clipper.util import ClipperError


def test_parse_json3_auto_captions_with_word_timings_and_speaker_change():
    data = {"events": [
        {"tStartMs": 0, "dDurationMs": 5000, "id": 1, "wWinId": 1},  # window definition, no segs
        {"tStartMs": 1000, "dDurationMs": 4000, "segs": [
            {"utf8": "&gt;&gt; Hello"}, {"utf8": " world", "tOffsetMs": 500}, {"utf8": " today", "tOffsetMs": 900}]},
        {"tStartMs": 2500, "dDurationMs": 100, "aAppend": 1, "segs": [{"utf8": "\n"}]},  # line break only
        {"tStartMs": 3000, "dDurationMs": 3000, "segs": [
            {"utf8": "[Music]"}, {"utf8": " next", "tOffsetMs": 400}, {"utf8": " line", "tOffsetMs": 800}]},
    ]}
    segs = parse_json3(data)
    assert [s["text"] for s in segs] == [">> Hello world today", "next line"]
    first = segs[0]
    assert first["start_sec"] == 1.0
    assert first["end_sec"] == 3.0  # clipped to the next event's start (rolling captions overlap)
    assert [w["text"] for w in first["words"]] == ["Hello", "world", "today"]
    assert first["words"][0]["speaker_change"] is True
    assert first["words"][1] == {"start": 1.5, "end": 1.9, "text": "world"}
    assert first["words"][2]["end"] == 3.0
    assert [w["text"] for w in segs[1]["words"]] == ["next", "line"]  # [Music] dropped


def test_parse_json3_manual_captions_have_no_word_timings():
    data = {"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "First line.\nwrapped"}]},
        {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "Second line."}]},
    ]}
    segs = parse_json3(data)
    assert segs[0] == {"start_sec": 0.0, "end_sec": 2.0, "text": "First line. wrapped", "words": []}
    assert segs[1]["text"] == "Second line."


def _track(ext="json3", url="u"):
    return [{"ext": "vtt", "url": "vtt"}, {"ext": ext, "url": url}]


def test_manual_subtitles_beat_auto_captions():
    info = {"subtitles": {"en-US": _track(url="manual")}, "automatic_captions": {"en-orig": _track(url="auto")}}
    assert pick_subtitle_track(info, "en") == ("manual", "en-US", "manual")


def test_auto_captions_prefer_original_track_over_translation():
    info = {"subtitles": {}, "automatic_captions": {"en": _track(url="translated"), "en-orig": _track(url="orig")}}
    assert pick_subtitle_track(info, "en") == ("auto", "en-orig", "orig")


def test_live_chat_is_not_a_subtitle_track():
    info = {"subtitles": {"live_chat": _track()}, "automatic_captions": {"en": _track(url="auto")}}
    assert pick_subtitle_track(info, "en")[0] == "auto"


def test_no_captions_is_a_clear_error():
    info = {"subtitles": {"de": _track()}, "automatic_captions": {}}
    with pytest.raises(ClipperError, match="No 'en' captions"):
        pick_subtitle_track(info, "en")


def test_heatmap_and_video_id():
    info = {"heatmap": [{"start_time": 0, "end_time": 10, "value": 0.4}, {"bad": 1}]}
    assert heatmap_from_info(info) == [{"start_sec": 0, "end_sec": 10, "value": 0.4}]
    assert heatmap_from_info({}) == []
    assert video_id_from_url("https://www.youtube.com/watch?v=yePLuwL3FbM&t=10") == "yePLuwL3FbM"
    assert video_id_from_url("https://youtu.be/yePLuwL3FbM") == "yePLuwL3FbM"
    assert video_id_from_url("yePLuwL3FbM") == "yePLuwL3FbM"
