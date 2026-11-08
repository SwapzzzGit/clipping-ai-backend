"""Frame-accurate review marks: an exact mark keeps its frame, and the editor can read a clip's frame rate."""

from fastapi.testclient import TestClient

from app import trims
from app.main import app, store
from clipper.util import parse_fps

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def _job_with_clip():
    clip = {"id": 1, "duration_sec": 30.0, "start_sec": 100.0, "end_sec": 130.0, "source_t0": 98.0,
            "review": {"approved": None, "notes": ""}}
    return {"status": "done", "picks": [{"id": 1, "start_sec": 100.0, "end_sec": 130.0}], "clips": [clip]}


def test_exact_mark_keeps_its_frame_and_is_clamped_to_the_pick():
    job = _job_with_clip()
    words = [{"start": 0.0, "end": 1.0, "text": "unused when snapping is off"}]
    exact = trims.add_mark(job, 1, "start", 3.4, words, snap=False)
    assert exact["exact"] is True and exact["source_sec"] == 101.4        # 98 + 3.4, inside the pick
    early = trims.add_mark(job, 1, "start", 0.5, words, snap=False)
    assert early["source_sec"] == 100.0                                    # 98.5 is before the pick, so clamp


def test_frame_rate_is_read_from_ffmpeg_output():
    assert parse_fps("  Stream #0:0(und): Video: h264 (High), yuv420p, 1080x1920, 2525 kb/s, 29.97 fps, 29.97 tbr") == 29.97
    assert parse_fps("  Stream #0:0: Video: h264, 1080x1920, 25 fps, 25 tbr") == 25.0
    assert parse_fps("  Stream #0:0: Audio: aac, 44100 Hz") is None


def test_timing_is_404_without_a_rendered_clip():
    job = store.create("https://youtu.be/TTTTTTTTTTT", "api", OPTIONS)
    assert client.get(f"/api/jobs/{job['id']}/clips/1/timing").status_code == 404
    assert client.get("/api/jobs/nope/clips/1/timing").status_code == 404
