"""Per-clip captions: a clip's own style changes how its captions are burned in, and only that clip is re-rendered."""

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.store import WORK_DIR
from clipper import pipeline
from clipper.caption_style import DEFAULTS, HIGHLIGHTS, caption_render
from clipper.captions import build_ass, group_words
from clipper.reframe import ANALYSIS_VERSION
from clipper.util import ClipperError, find_ffmpeg

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}
BASE = {"width": 1080, "height": 1920, "caption_font": "Arial", "caption_font_size": 84,
        "caption_highlight": "&H0000E5FF", "caption_words_per_line": 3}
RENDER = {"preset": "ultrafast", "crf": 30}


def _ffmpeg() -> str:
    try:
        return find_ffmpeg(None, WORK_DIR)
    except ClipperError:
        pytest.skip("ffmpeg not available")


def _words(texts: list[str], gap: float = 0.0) -> list[dict]:
    out, t = [], 0.0
    for text in texts:
        out.append({"start": t, "end": t + 0.4, "text": text})
        t += 0.4 + gap
    return out


def _job(status: str = "done") -> dict:
    job = store.create("https://youtu.be/CAPSTYLE001", "api", OPTIONS)
    store.update(job["id"], status=status, video={"id": "CAPSTYLE001", "title": "t", "channel": "c", "duration": 300,
                                                  "thumbnail": "", "captions": "auto", "heatmap": False},
                 picks=[{"id": 1, "start_sec": 100.0, "end_sec": 130.0, "title": "t", "hook_line": "h", "score": 5, "reason": "r"}],
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "t", "start_sec": 100.0, "end_sec": 130.0,
                         "duration_sec": 30.0, "source_t0": 99.9, "review": {"approved": None, "notes": ""}}])
    return job


# -- the settings --------------------------------------------------------------------------------

def test_a_clip_without_a_style_renders_with_the_job_settings():
    r = caption_render(BASE, None)
    assert r["caption_font"] == "Arial" and r["caption_font_size"] == 84
    assert r["caption_highlight"] == "&H0000E5FF" and r["caption_bold"] is True
    assert r["caption_animation"] == "words" and r["caption_max_chars"] == 0 and r["caption_shadow"] == 3


def test_a_clip_style_is_laid_over_the_job_settings():
    style = {"enabled": False, "font": "Impact", "size": 60, "bold": False, "animation": "lines",
             "max_chars": 12, "highlight": "cyan", "shadow": 5}
    r = caption_render(BASE, style)
    assert r["captions"] is False and r["caption_font"] == "Impact" and r["caption_font_size"] == 60
    assert r["caption_bold"] is False and r["caption_animation"] == "lines" and r["caption_max_chars"] == 12
    assert r["caption_highlight"] == HIGHLIGHTS["cyan"] and r["caption_shadow"] == 5
    assert BASE["caption_font"] == "Arial"  # the job's settings are not changed


def test_the_defaults_are_the_original_caption_look():
    assert DEFAULTS["size"] == 84 and DEFAULTS["shadow"] == 3 and DEFAULTS["bold"] is True
    assert HIGHLIGHTS[DEFAULTS["highlight"]] == BASE["caption_highlight"]


# -- line breaks ---------------------------------------------------------------------------------

def test_a_character_limit_breaks_the_line_before_it_is_passed():
    groups = group_words(_words(["alpha", "beta", "gamma", "delta"]), per_line=10, max_chars=10)
    assert [[w["text"] for w in g] for g in groups] == [["alpha", "beta"], ["gamma"], ["delta"]]


def test_without_a_character_limit_the_line_breaks_as_before():
    groups = group_words(_words(["alpha", "beta", "gamma", "delta"]), per_line=3)
    assert [len(g) for g in groups] == [3, 1]


def test_a_word_longer_than_the_limit_still_gets_a_line():
    groups = group_words(_words(["extraordinarily", "ok"]), per_line=3, max_chars=5)
    assert [[w["text"] for w in g] for g in groups] == [["extraordinarily"], ["ok"]]


# -- the caption file ----------------------------------------------------------------------------

def test_the_words_animation_highlights_the_spoken_word():
    r = caption_render(BASE, None)
    ass = build_ass(_words(["one", "two", "three"]), 0.0, 2.0, r)
    assert "{\\c&H0000E5FF}" in ass


def test_the_lines_animation_shows_each_line_without_highlight():
    r = caption_render(BASE, {"animation": "lines"})
    ass = build_ass(_words(["one", "two", "three", "four"]), 0.0, 2.0, r)
    dialogue = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogue) == 2  # 3 words, then one: one line each
    assert all("\\c&H" not in ln for ln in dialogue)


def test_bold_and_shadow_reach_the_style_line():
    r = caption_render(BASE, {"bold": False, "shadow": 6})
    style = next(ln for ln in build_ass(_words(["one"]), 0.0, 1.0, r).splitlines() if ln.startswith("Style:"))
    assert "&H96000000,0,0,0,0,100,100,0,0,1,7,6," in style


def test_the_default_style_line_is_unchanged():
    r = caption_render(BASE, None)
    style = next(ln for ln in build_ass(_words(["one"]), 0.0, 1.0, r).splitlines() if ln.startswith("Style:"))
    assert "&H96000000,-1,0,0,0,100,100,0,0,1,7,3," in style


# -- the endpoints -------------------------------------------------------------------------------

def test_saving_a_style_stores_it_on_the_clip_and_queues_a_re_render_of_that_clip():
    job = _job()
    r = client.put(f"/api/jobs/{job['id']}/clips/1/captions", json={"font": "Impact", "size": 60, "animation": "lines"})
    assert r.status_code == 200, r.text
    saved = store.get(job["id"])
    assert saved["picks"][0]["caption_style"]["font"] == "Impact"
    assert saved["picks"][0]["caption_style"]["animation"] == "lines"
    assert saved["status"] == "queued" and saved["clips"][0]["rerendering"] is True


def test_an_unknown_font_or_a_size_out_of_range_is_refused():
    job = _job()
    assert client.put(f"/api/jobs/{job['id']}/clips/1/captions", json={"font": "Comic Sans"}).status_code == 422
    assert client.put(f"/api/jobs/{job['id']}/clips/1/captions", json={"size": 500}).status_code == 422


def test_a_busy_job_refuses_a_new_style():
    job = _job(status="running")
    assert client.put(f"/api/jobs/{job['id']}/clips/1/captions", json={}).status_code == 409


def test_an_unknown_clip_is_not_found():
    job = _job()
    assert client.put(f"/api/jobs/{job['id']}/clips/9/captions", json={}).status_code == 404


def test_using_the_job_settings_again_clears_the_style():
    job = _job()
    client.put(f"/api/jobs/{job['id']}/clips/1/captions", json={"font": "Impact"})
    store.update(job["id"], status="done")  # the worker finishes the re-render
    r = client.delete(f"/api/jobs/{job['id']}/clips/1/captions")
    assert r.status_code == 200, r.text
    assert "caption_style" not in store.get(job["id"])["picks"][0]
    # Clearing a clip that has no style of its own is a no-op, even while the job is busy.
    store.update(job["id"], status="running")
    assert client.delete(f"/api/jobs/{job['id']}/clips/1/captions").status_code == 200


# -- the render ----------------------------------------------------------------------------------

def test_restyling_one_clip_re_renders_only_that_clip(tmp_path, monkeypatch):
    ff = _ffmpeg()
    work, out_dir = tmp_path / "work", tmp_path / "out"
    raw_path = tmp_path / "raw.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(raw_path)],
                   check=True, capture_output=True)
    (work / "analysis").mkdir(parents=True)
    (work / "analysis" / "raw.json").write_text(json.dumps({"version": ANALYSIS_VERSION}), encoding="utf-8")
    seen = []

    def fake_render(raw, clip, analysis, segments, out, cfg, ffmpeg):
        seen.append(clip.get("caption_style"))
        shutil.copy(raw_path, out)
        return {"duration_sec": 6.0, "source_t0": 100.0, "caption_words": 0, "thumbnail": None}

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    clip = {"id": 1, "title": "clip", "start_sec": 100.0, "end_sec": 106.0, "music": None, "branding": None}
    raws = {1: {"path": str(raw_path)}}
    pipeline.render_clips([clip], raws, work, out_dir, [], {"render": RENDER}, ff)
    pipeline.render_clips([clip], raws, work, out_dir, [], {"render": RENDER}, ff)
    assert len(seen) == 1  # same settings: the cached render is reused

    clip["caption_style"] = {"font": "Impact"}
    pipeline.render_clips([clip], raws, work, out_dir, [], {"render": RENDER}, ff)
    assert len(seen) == 2 and seen[1] == {"font": "Impact"}
