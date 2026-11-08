"""Text-based cuts: which words can be cut, how a clip is rendered without them, and what the editor is served."""

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import trims
from app.main import app, store
from app.store import WORK_DIR
from clipper import pipeline
from clipper.cuts import apply_cuts, cut_out, keep_ranges, probe_duration, waveform
from clipper.reframe import ANALYSIS_VERSION
from clipper.util import ClipperError, find_ffmpeg

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
RENDER = {"preset": "ultrafast", "crf": 30}
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def _ffmpeg() -> str:
    try:
        return find_ffmpeg(None, WORK_DIR)
    except ClipperError:
        pytest.skip("ffmpeg not available")


def _make_clip(path, seconds: float) -> None:
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=360x640:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True, capture_output=True)


def _job_with_clip(duration: float = 30.0) -> dict:
    clip = {"id": 1, "duration_sec": duration, "start_sec": 100.0, "end_sec": 130.0, "source_t0": 98.0,
            "review": {"approved": True, "notes": ""}, "cuts_baked": []}
    return {"status": "done", "picks": [{"id": 1, "start_sec": 100.0, "end_sec": 130.0}], "clips": [clip]}


def test_keep_ranges_removes_cuts_and_their_overlaps():
    assert keep_ranges(10.0, [(2.0, 3.0)]) == [(0.0, 2.0), (3.0, 10.0)]
    assert keep_ranges(10.0, [(2.0, 3.0), (3.0, 4.5)]) == [(0.0, 2.0), (4.5, 10.0)]
    assert keep_ranges(10.0, [(-1.0, 1.0), (9.5, 12.0)]) == [(1.0, 9.5)]
    assert keep_ranges(10.0, []) == [(0.0, 10.0)]


def test_cut_out_keeps_only_the_kept_parts(tmp_path):
    src = tmp_path / "clip.mp4"
    _make_clip(src, 6)
    dst = tmp_path / "cut.mp4"
    cut_out(_ffmpeg(), src, dst, [(0.0, 2.0), (3.0, 6.0)], RENDER)
    assert abs(probe_duration(_ffmpeg(), dst) - 5.0) < 0.15


def test_apply_cuts_uses_source_seconds_and_reports_the_new_length(tmp_path):
    out = tmp_path / "01_clip.mp4"
    _make_clip(out, 6)
    rendered = {"source_t0": 100.0, "duration_sec": 6.0, "thumbnail": None}
    done = apply_cuts(_ffmpeg(), out, rendered, [{"s": 101.0, "e": 103.0, "text": "middle"}], RENDER)
    assert done["duration_sec"] == 4.0
    assert done["cuts_baked"] == [{"s": 101.0, "e": 103.0, "text": "middle"}]
    assert abs(probe_duration(_ffmpeg(), out) - 4.0) < 0.15


def test_waveform_has_one_peak_per_step(tmp_path):
    clip = tmp_path / "w.mp4"
    _make_clip(clip, 3)
    data = waveform(_ffmpeg(), clip, step=0.1)
    assert data["step"] == 0.1
    assert 25 <= len(data["peaks"]) <= 35
    assert max(data["peaks"]) == 1.0


def test_a_render_cuts_the_words_out_and_reports_them(tmp_path, monkeypatch):
    ff = _ffmpeg()
    work, out_dir = tmp_path / "work", tmp_path / "out"
    raw_path = tmp_path / "raw.mp4"
    _make_clip(raw_path, 6)
    (work / "analysis").mkdir(parents=True)
    (work / "analysis" / "raw.json").write_text(json.dumps({"version": ANALYSIS_VERSION}), encoding="utf-8")

    def fake_render(raw, clip, analysis, segments, out, cfg, ffmpeg):
        shutil.copy(raw_path, out)
        return {"duration_sec": 6.0, "source_t0": 100.0, "caption_words": 0, "thumbnail": None}

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    clip = {"id": 1, "title": "clip", "start_sec": 100.0, "end_sec": 106.0, "music": None, "branding": None,
            "cuts": [{"s": 101.0, "e": 103.0, "text": "x"}]}
    got = pipeline.render_clips([clip], {1: {"path": str(raw_path)}}, work, out_dir, [], {"render": RENDER}, ff)[1]
    assert got["status"] == "ok" and got["duration_sec"] == 4.0
    assert got["cuts_baked"] == [{"s": 101.0, "e": 103.0, "text": "x"}]
    assert abs(probe_duration(ff, out_dir / got["file"]) - 4.0) < 0.15


def test_a_cut_is_kept_on_the_pick_and_can_be_restored():
    job = _job_with_clip()
    cut = trims.add_cut(job, 1, 103.0, 104.5, "Huh?")
    assert cut["s"] == 103.0 and cut["e"] == 104.5
    assert job["picks"][0]["cuts"] == [cut]
    trims.restore_cut(job, 1, 103.0, 104.5)
    assert job["picks"][0]["cuts"] == [] and job["clips"][0]["review"]["cuts"] == []


def test_cuts_stay_inside_the_clip_and_leave_three_seconds():
    job = _job_with_clip()
    with pytest.raises(trims.TrimError):
        trims.add_cut(job, 1, 99.0, 100.5, "early")
    trims.add_cut(job, 1, 101.0, 103.0, "a")
    with pytest.raises(trims.TrimError) as overlap:
        trims.add_cut(job, 1, 102.5, 104.0, "b")
    assert overlap.value.status == 409
    with pytest.raises(trims.TrimError):
        trims.add_cut(job, 1, 103.0, 128.5, "too much")  # would leave 2.5 s


def test_a_cut_only_re_render_needs_a_change_and_clears_approval():
    job = _job_with_clip()
    with pytest.raises(trims.TrimError):
        trims.apply_trim(job, 1, [])  # nothing to re-render yet
    trims.add_cut(job, 1, 103.0, 104.5, "Huh?")
    old, new = trims.apply_trim(job, 1, [])
    assert old == new == (100.0, 130.0)
    assert job["clips"][0]["review"]["approved"] is None and job["clips"][0]["rerendering"] is True
    job["clips"][0]["cuts_baked"] = [{"s": 103.0, "e": 104.5, "text": "Huh?"}]  # the file now holds the cut
    job["clips"][0]["rerendering"] = False
    with pytest.raises(trims.TrimError):
        trims.apply_trim(job, 1, [])


def test_the_words_endpoint_lists_the_clip_in_source_seconds_and_takes_a_cut():
    vid = "CUTSTEST001"
    folder = WORK_DIR / vid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.json").write_text(json.dumps({"segments": [
        {"start_sec": 100.0, "end_sec": 104.0, "text": "one two three four", "words": [
            {"start": 100.0, "end": 100.5, "text": "one"}, {"start": 100.5, "end": 101.0, "text": "two"},
            {"start": 101.0, "end": 102.0, "text": "three"}, {"start": 102.0, "end": 103.5, "text": "four"}]},
        {"start_sec": 140.0, "end_sec": 141.0, "text": "outside", "words": []}]}), encoding="utf-8")
    job = store.create("https://youtu.be/CUTSTEST001", "api", OPTIONS)
    store.update(job["id"], video={"id": vid, "title": "t", "channel": "c", "duration": 300, "thumbnail": "",
                                   "captions": "auto", "heatmap": False},
                 status="done",
                 picks=[{"id": 1, "start_sec": 100.0, "end_sec": 104.0, "title": "t", "hook_line": "h", "score": 5, "reason": "r"}],
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "t", "start_sec": 100.0, "end_sec": 104.0,
                         "duration_sec": 4.0, "source_t0": 99.9, "review": {"approved": None, "notes": ""}, "cuts_baked": []}])
    got = client.get(f"/api/jobs/{job['id']}/clips/1/words").json()
    assert [w["text"] for w in got["words"]] == ["one", "two", "three", "four"]
    assert {k: v for k, v in got["words"][2].items() if k != "k"} == {"i": 2, "text": "three", "s": 101.0, "e": 102.0}
    assert got["words"][2]["k"] == "101.000"  # the key a caption edit uses for this word
    assert got["source_t0"] == 99.9
    r = client.post(f"/api/jobs/{job['id']}/clips/1/cuts", json={"s": 101.0, "e": 102.0, "text": "three"})
    assert r.status_code == 200, r.text
    clip = next(c for c in r.json()["clips"] if c["id"] == 1)
    assert clip["review"]["cuts"][0]["text"] == "three"
