"""Caption text: a reviewer corrects a word in the captions. The word keeps its timing, only its text changes,
and only that clip is re-rendered."""

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.store import WORK_DIR
from clipper import pipeline
from clipper.caption_style import caption_render
from clipper.captions import apply_caption_edits, word_key, write_ass
from clipper.reframe import ANALYSIS_VERSION
from clipper.util import ClipperError, find_ffmpeg

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}
BASE = {"width": 1080, "height": 1920, "caption_font": "Arial", "caption_font_size": 84,
        "caption_highlight": "&H0000E5FF", "caption_words_per_line": 3}
RENDER = {"preset": "ultrafast", "crf": 30}
TEXTS = ["Helo", "world", "today"]
SEGMENTS = [{"start_sec": 100.0, "end_sec": 102.0, "text": " ".join(TEXTS), "words": [
    {"start": 100.0, "end": 100.5, "text": "Helo"}, {"start": 100.5, "end": 101.0, "text": "world"},
    {"start": 101.0, "end": 101.5, "text": "today"}]}]


def _words() -> list[dict]:
    return SEGMENTS[0]["words"]


def _job(status: str = "done") -> dict:
    job = store.create("https://youtu.be/CAPTEXT0001", "api", OPTIONS)
    store.update(job["id"], status=status, video={"id": "CAPTEXT0001", "title": "t", "channel": "c", "duration": 300,
                                                  "thumbnail": "", "captions": "auto", "heatmap": False},
                 picks=[{"id": 1, "start_sec": 100.0, "end_sec": 102.0, "title": "t", "hook_line": "h", "score": 5, "reason": "r"}],
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "t", "start_sec": 100.0, "end_sec": 102.0,
                         "duration_sec": 2.0, "source_t0": 99.9, "review": {"approved": None, "notes": ""}}])
    return job


# -- the words -----------------------------------------------------------------------------------

def test_a_word_is_found_by_its_start_to_the_millisecond():
    assert word_key(100.0) == "100.000"
    assert word_key(101.2345) == "101.234"


def test_a_corrected_word_takes_the_new_text_and_keeps_its_timing():
    out = apply_caption_edits(_words(), {"100.000": "Hello"})
    assert out[0] == {"start": 100.0, "end": 100.5, "text": "Hello"}
    assert out[1:] == _words()[1:]


def test_a_word_corrected_to_nothing_is_left_out():
    out = apply_caption_edits(_words(), {"100.500": ""})
    assert [w["text"] for w in out] == ["Helo", "today"]


def test_no_edits_leave_the_words_alone():
    assert apply_caption_edits(_words(), None) == _words()
    assert apply_caption_edits(_words(), {}) == _words()


# -- the caption file ----------------------------------------------------------------------------

def test_the_caption_file_shows_the_corrected_word(tmp_path):
    path = tmp_path / "clip.ass"
    r = caption_render(BASE, None)
    n = write_ass(path, SEGMENTS, 100.0, 102.0, 100.0, r, edits={"100.000": "Hello"})
    text = path.read_text(encoding="utf-8")
    assert n == 3
    assert "Hello" in text and "Helo" not in text
    assert "world" in text and "today" in text


def test_a_word_removed_from_the_captions_is_not_written(tmp_path):
    path = tmp_path / "clip.ass"
    n = write_ass(path, SEGMENTS, 100.0, 102.0, 100.0, caption_render(BASE, None), edits={"100.500": ""})
    text = path.read_text(encoding="utf-8")
    assert n == 2
    assert "world" not in text


# -- the endpoint --------------------------------------------------------------------------------

def test_saving_corrections_stores_them_on_the_clip_and_queues_its_re_render():
    job = _job()
    r = client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "Hello"}})
    assert r.status_code == 200, r.text
    saved = store.get(job["id"])
    assert saved["picks"][0]["caption_text"] == {"100.000": "Hello"}
    assert saved["status"] == "queued" and saved["clips"][0]["rerendering"] is True


def test_saved_text_has_its_spaces_tidied():
    job = _job()
    client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "  Hello   there "}})
    assert store.get(job["id"])["picks"][0]["caption_text"] == {"100.000": "Hello there"}


def test_a_key_that_is_not_a_word_or_a_long_correction_is_refused():
    job = _job()
    assert client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"word": "x"}}).status_code == 422
    assert client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "x" * 61}}).status_code == 422


def test_saving_the_same_corrections_does_not_re_render():
    job = _job()
    client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "Hello"}})
    store.update(job["id"], status="done")  # the worker finishes the re-render
    r = client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "Hello"}})
    assert r.status_code == 200
    assert store.get(job["id"])["status"] == "done"


def test_an_empty_list_of_corrections_clears_them():
    job = _job()
    client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "Hello"}})
    store.update(job["id"], status="done")
    client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {}})
    assert "caption_text" not in store.get(job["id"])["picks"][0]


def test_a_busy_job_refuses_corrections():
    job = _job(status="running")
    assert client.put(f"/api/jobs/{job['id']}/clips/1/caption-text", json={"edits": {"100.000": "x"}}).status_code == 409


def test_an_unknown_clip_is_not_found():
    job = _job()
    assert client.put(f"/api/jobs/{job['id']}/clips/9/caption-text", json={"edits": {}}).status_code == 404


# -- the render ----------------------------------------------------------------------------------

def test_correcting_a_word_re_renders_only_that_clip(tmp_path, monkeypatch):
    try:
        ff = find_ffmpeg(None, WORK_DIR)
    except ClipperError:
        pytest.skip("ffmpeg not available")
    work, out_dir = tmp_path / "work", tmp_path / "out"
    raw_path = tmp_path / "raw.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(raw_path)],
                   check=True, capture_output=True)
    (work / "analysis").mkdir(parents=True)
    (work / "analysis" / "raw.json").write_text(json.dumps({"version": ANALYSIS_VERSION}), encoding="utf-8")
    seen = []

    def fake_render(raw, clip, analysis, segments, out, cfg, ffmpeg):
        seen.append(clip.get("caption_text"))
        shutil.copy(raw_path, out)
        return {"duration_sec": 6.0, "source_t0": 100.0, "caption_words": 0, "thumbnail": None}

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    clip = {"id": 1, "title": "clip", "start_sec": 100.0, "end_sec": 106.0, "music": None, "branding": None}
    raws = {1: {"path": str(raw_path)}}
    pipeline.render_clips([clip], raws, work, out_dir, [], {"render": RENDER}, ff)
    pipeline.render_clips([clip], raws, work, out_dir, [], {"render": RENDER}, ff)
    assert len(seen) == 1  # same text: the cached render is reused

    clip["caption_text"] = {"100.000": "Hello"}
    pipeline.render_clips([clip], raws, work, out_dir, [], {"render": RENDER}, ff)
    assert len(seen) == 2 and seen[1] == {"100.000": "Hello"}
