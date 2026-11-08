"""Download all: a job's rendered clips come back as one zip."""

import io
import shutil
import zipfile

from fastapi.testclient import TestClient

from app.main import app, store
from app.store import OUTPUT_DIR

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 2, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def test_download_all_zips_the_rendered_clips_and_nothing_else():
    job = store.create("https://youtu.be/ZZZZZZZZZZZ", "api", OPTIONS)
    folder = OUTPUT_DIR / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / "clip_01.mp4").write_bytes(b"one")
        (folder / "clip_02.mp4").write_bytes(b"two")
        store.update(job["id"], clips=[{"file": "clip_01.mp4"}, {"file": None}, {"file": "clip_02.mp4"}])
        r = client.get(f"/api/jobs/{job['id']}/clips.zip")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        assert "clips.zip" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            assert sorted(z.namelist()) == ["clip_01.mp4", "clip_02.mp4"]
            assert z.read("clip_02.mp4") == b"two"
        assert not list(folder.glob("*.zip"))  # the temporary archive is deleted once it is sent
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_download_all_needs_a_job_with_a_rendered_clip():
    assert client.get("/api/jobs/nope/clips.zip").status_code == 404
    job = store.create("https://youtu.be/YYYYYYYYYYY", "api", OPTIONS)
    assert client.get(f"/api/jobs/{job['id']}/clips.zip").status_code == 404
