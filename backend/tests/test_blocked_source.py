"""A job that YouTube blocked can continue with a video the person downloaded and uploaded."""

from fastapi.testclient import TestClient

from app import worker as worker_module
from app.main import app, store, worker
from clipper.download import BOT_BLOCK_HELP
from clipper.util import ClipperError

# Local host + loopback client: the publisher tunnel guard 404s anything that looks external.
client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def test_a_blocked_job_continues_with_an_uploaded_video():
    url = "https://youtu.be/GGGGGGGGGGG"
    job = store.create(url, "api", OPTIONS)
    store.update(job["id"], status="failed", error=BOT_BLOCK_HELP, blocked=True, picks=[{"id": 1}], clips=[])
    up = client.post("/api/uploads?name=mine.mp4", content=b"\x00" * 1000,
                     headers={"content-type": "application/octet-stream"}).json()

    url_path = f"/api/jobs/{job['id']}/source"
    assert client.post(url_path, json={"upload_url": up["url"], "rights_confirmed": False}).status_code == 422
    r = client.post(url_path, json={"upload_url": up["url"], "rights_confirmed": True})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["url"] == up["url"] and got["original_url"] == url and got["status"] == "queued"
    assert got["picks"] is None and got["blocked"] is False and got["rights_confirmed_at"]
    assert client.post(url_path, json={"upload_url": up["url"], "rights_confirmed": True}).status_code == 409  # not failed now


def test_only_a_blocked_job_can_take_an_uploaded_video():
    up = client.post("/api/uploads?name=other.mp4", content=b"\x00" * 1000,
                     headers={"content-type": "application/octet-stream"}).json()
    job = store.create("https://youtu.be/FFFFFFFFFFF", "api", OPTIONS)
    store.update(job["id"], status="failed", error="Could not read video info: Video unavailable")
    body = {"upload_url": up["url"], "rights_confirmed": True}
    assert client.post(f"/api/jobs/{job['id']}/source", json=body).status_code == 409


def test_youtube_bot_check_while_reading_captions_marks_the_job_blocked(monkeypatch):
    job = store.create("https://youtu.be/HHHHHHHHHHH", "api", OPTIONS)

    def blocked_info(url, work_root):
        raise ClipperError("Could not read video info: ERROR: [youtube] HHHHHHHHHHH: "
                           "Sign in to confirm you're not a bot.")

    monkeypatch.setattr(worker_module, "find_ffmpeg", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(worker_module, "load_or_fetch_info", blocked_info)
    worker._run(job["id"])
    got = store.get(job["id"])
    assert got["status"] == "failed" and got["blocked"] is True and got["error"] == BOT_BLOCK_HELP
