"""The full editor's export becomes the clip's video: stored beside the original, probed, and it clears approval."""

import subprocess

from fastapi.testclient import TestClient

from app.main import app, store
from app.store import OUTPUT_DIR, WORK_DIR
from clipper.util import find_ffmpeg

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def _mp4_bytes(tmp_path, seconds: float) -> bytes:
    out = tmp_path / "export.mp4"
    subprocess.run([find_ffmpeg(None, WORK_DIR), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=360x640:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)], check=True, capture_output=True)
    return out.read_bytes()


def _job_with_rendered_clip(status: str = "done") -> dict:
    job = store.create("https://youtu.be/EDITEDCLIP1", "api", OPTIONS)
    folder = OUTPUT_DIR / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01_clip.mp4").write_bytes(b"original render")
    store.update(job["id"], status=status, picks=[{"id": 1, "start_sec": 100.0, "end_sec": 104.0}],
                 clips=[{"id": 1, "status": "ok", "file": "01_clip.mp4", "title": "clip", "start_sec": 100.0,
                         "end_sec": 104.0, "duration_sec": 4.0, "review": {"approved": True, "notes": "",
                                                                             "approved_by": "person"}}])
    return job


def _post(job_id: str, body: bytes, clip_id: int = 1):
    return client.post(f"/api/jobs/{job_id}/clips/{clip_id}/edited", content=body,
                       headers={"content-type": "video/mp4"})


def test_the_export_becomes_the_clip_and_the_original_stays(tmp_path):
    job = _job_with_rendered_clip()
    r = _post(job["id"], _mp4_bytes(tmp_path, 6.0))
    assert r.status_code == 200, r.text
    clip = next(c for c in r.json()["clips"] if c["id"] == 1)
    assert clip["file"].startswith("01_clip_edited_") and clip["file"].endswith(".mp4")
    assert (OUTPUT_DIR / job["id"] / clip["file"]).exists()
    assert (OUTPUT_DIR / job["id"] / "01_clip.mp4").read_bytes() == b"original render"
    assert abs(clip["duration_sec"] - 6.0) < 0.2
    assert clip["edited_in_editor"] is True
    assert clip["review"]["approved"] is None and clip["review"]["approved_by"] is None


def test_an_edit_of_an_edit_keeps_the_original_stem(tmp_path):
    job = _job_with_rendered_clip()
    first = next(c for c in _post(job["id"], _mp4_bytes(tmp_path, 5.0)).json()["clips"] if c["id"] == 1)
    second = next(c for c in _post(job["id"], _mp4_bytes(tmp_path, 3.0)).json()["clips"] if c["id"] == 1)
    assert second["file"].startswith("01_clip_edited_")
    assert second["file"].count("_edited") == 1
    assert first["file"] != second["file"]


def test_a_file_that_is_not_video_is_refused_and_leaves_no_file():
    job = _job_with_rendered_clip()
    r = _post(job["id"], b"this is not a video")
    assert r.status_code == 422
    assert not list((OUTPUT_DIR / job["id"]).glob("*_edited_*.mp4"))
    clip = next(c for c in store.get(job["id"])["clips"] if c["id"] == 1)
    assert clip["file"] == "01_clip.mp4"


def test_an_empty_body_is_refused(tmp_path):
    job = _job_with_rendered_clip()
    assert _post(job["id"], b"").status_code == 422


def test_a_busy_job_refuses_the_export(tmp_path):
    job = _job_with_rendered_clip(status="running")
    assert _post(job["id"], _mp4_bytes(tmp_path, 3.0)).status_code == 409


def test_an_unknown_clip_is_not_found(tmp_path):
    job = _job_with_rendered_clip()
    assert _post(job["id"], _mp4_bytes(tmp_path, 3.0), clip_id=99).status_code == 404
