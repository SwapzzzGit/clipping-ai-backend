"""A clip's name is set in the editor: it is trimmed, kept to 120 characters, and must not be empty."""

from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}


def _job() -> dict:
    job = store.create("https://youtu.be/CLIPTITLE01", "api", OPTIONS)
    store.update(job["id"], status="done",
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "Old name", "review": {"approved": None, "notes": ""}}])
    return job


def test_the_clip_takes_the_new_name_without_surrounding_spaces():
    job = _job()
    r = client.patch(f"/api/jobs/{job['id']}/clips/1/title", json={"title": "  Better hook  "})
    assert r.status_code == 200, r.text
    assert r.json()["clips"][0]["title"] == "Better hook"


def test_a_blank_name_is_refused_and_the_old_name_stays():
    job = _job()
    assert client.patch(f"/api/jobs/{job['id']}/clips/1/title", json={"title": "   "}).status_code == 422
    assert store.get(job["id"])["clips"][0]["title"] == "Old name"


def test_a_long_name_is_kept_to_120_characters():
    job = _job()
    r = client.patch(f"/api/jobs/{job['id']}/clips/1/title", json={"title": "x" * 300})
    assert len(r.json()["clips"][0]["title"]) == 120


def test_an_unknown_clip_is_not_found():
    job = _job()
    assert client.patch(f"/api/jobs/{job['id']}/clips/9/title", json={"title": "Name"}).status_code == 404
