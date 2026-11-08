"""Caption styles from pycaps: the words come from our transcript, the look from a pycaps template.
A clip can be re-captioned in another style, and only that clip is re-rendered."""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from clipper.captions import word_key
from clipper.pycaps_captions import (DEFAULT_TEMPLATE, OURS, PREVIEW_DIR, caption_words, clip_caption_template,
                                     is_valid, prepared_template, pycaps_document, style_options, template_names)
from clipper.util import find_ffmpeg
from pathlib import Path

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}
WORDS = [
    {"start": 100.0, "end": 100.5, "text": "Helo"},
    {"start": 100.5, "end": 101.0, "text": "world"},
    {"start": 101.0, "end": 101.5, "text": "today"},
]
SEGMENTS = [{"start_sec": 100.0, "end_sec": 102.0, "text": "Helo world today", "words": WORDS}]


# -- the styles on offer -------------------------------------------------------------------------

def test_every_builtin_pycaps_style_is_offered_except_the_template_writer():
    names = template_names()
    assert names == sorted(names)
    assert {"classic", "default", "explosive", "fast", "hype", "line-focus", "minimalist",
            "neo-minimal", "retro-gaming", "vibrant", "word-focus"} == set(names)
    assert "model" not in names


def test_ours_comes_first_and_every_pycaps_style_has_a_preview_path():
    options = style_options()
    assert options[0]["name"] == OURS and options[0]["preview"] is None
    for option in options[1:]:
        assert option["preview"] == f"/api/caption-previews/{option['name']}"


def test_the_default_style_is_a_valid_one():
    assert DEFAULT_TEMPLATE in template_names()
    assert is_valid(DEFAULT_TEMPLATE) and is_valid(OURS)
    assert not is_valid("no-such-style")


def test_a_clip_uses_its_own_style_before_the_job_default():
    cfg = {"render": {"caption_template": "vibrant"}}
    assert clip_caption_template({"caption_template": "minimalist"}, cfg) == "minimalist"
    assert clip_caption_template({}, cfg) == "vibrant"
    assert clip_caption_template({}, {"render": {}}) == DEFAULT_TEMPLATE


def test_the_explosive_style_loses_its_sound_effect(tmp_path):
    template = prepared_template("explosive", tmp_path)
    import json
    data = json.loads(Path(template.get_json_path()).read_text(encoding="utf-8"))
    assert "sound_effects" not in data
    assert Path(template.get_json_path()).parent.joinpath("styles.css").is_file()


# -- from our words to pycaps' words ---------------------------------------------------------------

def test_words_are_timed_from_the_start_of_the_rendered_clip():
    doc = pycaps_document(WORDS, t0=99.9, duration=2.0)
    first = doc["segments"][0]["words"][0]
    assert first == {"text": "Helo", "start": 0.1, "end": 0.6}


def test_words_after_the_end_of_the_clip_are_left_out_and_the_last_one_is_cut_short():
    doc = pycaps_document(WORDS, t0=100.0, duration=0.8)
    texts = [w["text"] for s in doc["segments"] for w in s["words"]]
    assert texts == ["Helo", "world"]
    assert doc["segments"][-1]["words"][-1]["end"] <= 0.8


def test_words_never_overlap_and_stay_in_order():
    overlapping = [{"start": 1.0, "end": 1.6, "text": "a"}, {"start": 1.2, "end": 1.9, "text": "b"}]
    words = [w for s in pycaps_document(overlapping, t0=0.0, duration=5.0)["segments"] for w in s["words"]]
    assert words[1]["start"] >= words[0]["end"]
    assert all(w["end"] > w["start"] for w in words)


def test_a_corrected_word_is_drawn_with_its_new_text_and_a_skipped_span_is_not():
    edits = {word_key(100.0): "Hello"}
    skip = [(101.0, 101.5)]
    words = caption_words(SEGMENTS, 100.0, 102.0, edits=edits, skip=skip)
    assert [w["text"] for w in words] == ["Hello", "world"]


# -- the clip itself -------------------------------------------------------------------------------

def _job(status: str = "done") -> dict:
    job = store.create("https://youtu.be/CAPSTYLE001", "api", OPTIONS)
    store.update(job["id"], status=status, video={"id": "CAPSTYLE001", "title": "t", "channel": "c", "duration": 300,
                                                  "thumbnail": "", "captions": "auto", "heatmap": False},
                 picks=[{"id": 1, "start_sec": 100.0, "end_sec": 102.0, "title": "t", "hook_line": "h", "score": 5,
                         "reason": "r"}],
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "t", "start_sec": 100.0, "end_sec": 102.0,
                         "duration_sec": 2.0, "source_t0": 99.9, "review": {"approved": None, "notes": ""}}])
    return job


def test_the_style_list_is_served_with_the_default():
    body = client.get("/api/caption-styles").json()
    assert body["default"] == DEFAULT_TEMPLATE
    assert [s["name"] for s in body["styles"]][0] == OURS


def test_an_unknown_style_is_refused():
    job = _job()
    r = client.put(f"/api/jobs/{job['id']}/clips/1/caption-template", json={"template": "no-such-style"})
    assert r.status_code == 422


def test_choosing_a_style_saves_it_and_queues_only_that_clip_for_re_rendering():
    job = _job(status="done")
    r = client.put(f"/api/jobs/{job['id']}/clips/1/caption-template", json={"template": "minimalist"})
    assert r.status_code == 200
    saved = store.get(job["id"])
    assert saved["picks"][0]["caption_template"] == "minimalist"
    assert saved["clips"][0]["rerendering"] is True
    assert saved["status"] == "queued"


def test_choosing_the_style_a_clip_already_has_changes_nothing():
    job = _job(status="done")
    client.put(f"/api/jobs/{job['id']}/clips/1/caption-template", json={"template": "vibrant"})
    store.update(job["id"], status="done")
    client.put(f"/api/jobs/{job['id']}/clips/1/caption-template", json={"template": "vibrant"})
    assert store.get(job["id"])["status"] == "done"


def test_a_busy_job_refuses_a_new_style():
    job = _job(status="running")
    r = client.put(f"/api/jobs/{job['id']}/clips/1/caption-template", json={"template": "hype"})
    assert r.status_code == 409


def test_a_preview_is_served_only_for_a_real_style():
    assert client.get("/api/caption-previews/no-such-style").status_code == 404
    assert client.get(f"/api/caption-previews/{OURS}").status_code == 404
    if (PREVIEW_DIR / f"{DEFAULT_TEMPLATE}.jpg").is_file():
        r = client.get(f"/api/caption-previews/{DEFAULT_TEMPLATE}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


# -- a real render (slow: pycaps drives a browser) --------------------------------------------------

@pytest.mark.slow
def test_a_pycaps_render_burns_the_captions_and_keeps_the_sound(tmp_path):
    ffmpeg = find_ffmpeg(cache_dir=tmp_path)
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=540x960:rate=25:duration=3", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)], check=True)
    from clipper.pycaps_captions import render_pycaps
    out = tmp_path / "out.mp4"
    doc = pycaps_document(WORDS, t0=100.0, duration=3.0)
    render_pycaps(source, out, doc, DEFAULT_TEMPLATE, tmp_path)
    assert out.is_file() and out.stat().st_size > 1000
    ffprobe = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    probe = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert "audio" in probe.stdout
    shutil.rmtree(tmp_path / "caption-templates", ignore_errors=True)


@pytest.mark.slow
def test_a_second_render_replaces_the_file_and_a_failed_one_keeps_it(tmp_path):
    """A clip re-rendered in another style replaces its file. A render that fails leaves the old file alone."""
    from clipper.pycaps_captions import render_pycaps
    ffmpeg = find_ffmpeg(cache_dir=tmp_path)
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=540x960:rate=25:duration=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)], check=True)
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"previous render")
    doc = pycaps_document(WORDS, t0=100.0, duration=2.0)
    render_pycaps(source, out, doc, "minimalist", tmp_path)
    assert out.stat().st_size > 1000 and out.read_bytes() != b"previous render"
    previous = out.read_bytes()
    with pytest.raises(Exception):
        render_pycaps(source, out, doc, "no-such-style", tmp_path)
    assert out.read_bytes() == previous
    assert not list(tmp_path.glob("*.captioning.mp4"))
