from fastapi.testclient import TestClient

from app.main import app, store
from app.store import OUTPUT_DIR

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))

MANIFEST = {
    "video": {"id": "vidX", "url": "https://youtu.be/vidX", "title": "Talk", "channel": "Chan", "duration_sec": 900},
    "settings": {"clips": {"min_sec": 20, "max_sec": 60}},
    "clips": [{"id": 1, "status": "ok", "file": "01_a.mp4", "thumbnail": "01_a.jpg", "title": "A",
               "duration_sec": 30.0, "review": {"approved": None, "notes": ""}},
              {"id": 2, "status": "failed", "file": None}],
}


def test_import_flow_creates_a_done_job(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    r = client.post("/api/import/jobs", json={"manifest": MANIFEST})
    assert r.status_code == 201
    job_id, files = r.json()["job_id"], r.json()["files"]
    assert files == ["01_a.mp4", "01_a.jpg"]
    assert client.post(f"/api/import/jobs/{job_id}/finish").status_code == 409          # files missing
    assert client.put(f"/api/import/jobs/{job_id}/files/evil.mp4", content=b"x").status_code == 422
    for name in files:
        assert client.put(f"/api/import/jobs/{job_id}/files/{name}", content=b"data-" + name.encode()).status_code == 200
    job = client.post(f"/api/import/jobs/{job_id}/finish").json()
    assert job["status"] == "done" and [c["id"] for c in job["clips"]] == [1]
    assert (OUTPUT_DIR / job_id / "manifest.json").is_file()
    assert (OUTPUT_DIR / job_id / "01_a.mp4").read_bytes() == b"data-01_a.mp4"
    store.delete(job_id)


def test_import_needs_auth_and_accepts_bearer(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw-123")
    assert client.post("/api/import/jobs", json={"manifest": MANIFEST}).status_code == 401
    r = client.post("/api/import/jobs", json={"manifest": MANIFEST}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = client.post("/api/import/jobs", json={"manifest": MANIFEST}, headers={"Authorization": "Bearer pw-123"})
    assert r.status_code == 201
    store.delete(r.json()["job_id"])


def test_rejects_unsafe_names(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    bad = {**MANIFEST, "clips": [{"id": 1, "status": "ok", "file": "../../etc/passwd.mp4"}]}
    assert client.post("/api/import/jobs", json={"manifest": bad}).status_code == 422
