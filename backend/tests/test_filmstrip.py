"""Thumbnails for the video lane: one picture every second for a short clip, made once and then reused."""

import subprocess

from fastapi.testclient import TestClient

from app.main import app, store
from app.store import OUTPUT_DIR, WORK_DIR
from clipper.strip import filmstrip
from clipper.util import find_ffmpeg

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def _make_clip(path, seconds: float) -> None:
    subprocess.run([find_ffmpeg(None, WORK_DIR), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=360x640:rate=30:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)


def test_a_six_second_clip_gets_about_one_picture_per_second(tmp_path):
    src = tmp_path / "01_clip.mp4"
    _make_clip(src, 6.0)
    meta = filmstrip(find_ffmpeg(None, WORK_DIR), src, 6.0)
    assert meta["interval"] == 1.0
    assert 5 <= meta["count"] <= 7
    assert (tmp_path / "01_clip_strip_001.jpg").is_file()


def test_the_pictures_are_reused_and_not_made_again(tmp_path):
    src = tmp_path / "02_clip.mp4"
    _make_clip(src, 3.0)
    ff = find_ffmpeg(None, WORK_DIR)
    first = filmstrip(ff, src, 3.0)
    picture = tmp_path / "02_clip_strip_001.jpg"
    stamp = picture.stat().st_mtime_ns
    assert filmstrip(ff, src, 3.0) == first
    assert picture.stat().st_mtime_ns == stamp


def test_the_endpoint_serves_the_thumbnails_of_a_rendered_clip():
    job = store.create("https://youtu.be/FILMSTRIP01", "api", OPTIONS)
    folder = OUTPUT_DIR / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    _make_clip(folder / "01_film.mp4", 4.0)
    store.update(job["id"], status="done",
                 clips=[{"id": 1, "status": "ok", "file": "01_film.mp4", "title": "t", "duration_sec": 4.0,
                         "review": {"approved": None, "notes": ""}}])
    r = client.get(f"/api/jobs/{job['id']}/clips/1/filmstrip")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stem"] == "01_film" and body["count"] >= 3
    assert (folder / "01_film_strip_001.jpg").is_file()


def test_a_clip_without_a_render_has_no_thumbnails():
    job = store.create("https://youtu.be/FILMSTRIP02", "api", OPTIONS)
    store.update(job["id"], status="done",
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "t", "review": {"approved": None, "notes": ""}}])
    assert client.get(f"/api/jobs/{job['id']}/clips/1/filmstrip").status_code == 404
