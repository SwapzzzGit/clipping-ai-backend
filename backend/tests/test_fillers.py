"""Filler words: the list's words are cut from the clip, turning the setting off restores only those cuts,
and the reviewer's own cuts are never touched."""

import json

from fastapi.testclient import TestClient

from app import trims
from app.main import app, store
from app.store import WORK_DIR

client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off
OPTIONS = {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True}
VID = "FILLERTEST01"
WORDS = [("So", 100.0, 100.4), ("you", 100.4, 100.8), ("wake", 100.8, 101.2), ("up.", 101.2, 101.6),
         ("Huh?", 101.6, 102.2), ("so", 102.2, 102.6), ("I", 102.6, 103.0), ("ain't", 103.0, 103.5), ("happy", 103.5, 104.0)]


def _segments() -> list[dict]:
    folder = WORK_DIR / VID
    folder.mkdir(parents=True, exist_ok=True)
    words = [{"start": s, "end": e, "text": t} for t, s, e in WORDS]
    seg = {"start_sec": 100.0, "end_sec": 104.0, "text": " ".join(t for t, _, _ in WORDS), "words": words}
    (folder / "transcript.json").write_text(json.dumps({"segments": [seg]}), encoding="utf-8")
    return [seg]


def _job(end: float = 110.0, status: str = "done") -> dict:
    _segments()
    job = store.create("https://youtu.be/FILLERTEST01", "api", OPTIONS)
    store.update(job["id"], status=status,
                 video={"id": VID, "title": "t", "channel": "c", "duration": 300, "thumbnail": "",
                        "captions": "auto", "heatmap": False},
                 picks=[{"id": 1, "start_sec": 100.0, "end_sec": end, "title": "t", "hook_line": "h", "score": 5, "reason": "r"}],
                 clips=[{"id": 1, "status": "ok", "file": None, "title": "t", "start_sec": 100.0, "end_sec": end,
                         "duration_sec": end - 100.0, "source_t0": 99.9,
                         "review": {"approved": None, "notes": "", "cuts": []}, "cuts_baked": []}])
    return job


def _cuts(job_id: str) -> list[dict]:
    return store.get(job_id)["clips"][0]["review"]["cuts"]


def test_turning_the_setting_on_cuts_each_filler_word():
    job = _job()
    r = client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["so"]})
    assert r.status_code == 200, r.text
    cuts = _cuts(job["id"])
    assert [c["text"] for c in cuts] == ["So", "so"]
    assert all(c["auto"] == "filler" for c in cuts)
    assert store.get(job["id"])["clips"][0]["review"]["fillers"] == {"on": True, "words": ["so"]}


def test_turning_it_off_removes_only_the_filler_cuts():
    job = _job()
    client.post(f"/api/jobs/{job['id']}/clips/1/cuts", json={"s": 101.6, "e": 102.2, "text": "Huh?"})
    client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["so"]})
    assert len(_cuts(job["id"])) == 3
    client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": False, "words": ["so"]})
    assert [c["text"] for c in _cuts(job["id"])] == ["Huh?"]


def test_a_filler_under_a_manual_cut_is_skipped():
    job = _job()
    client.post(f"/api/jobs/{job['id']}/clips/1/cuts", json={"s": 102.0, "e": 102.8, "text": "so I"})
    client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["so"]})
    cuts = _cuts(job["id"])
    assert [c["text"] for c in cuts if c.get("auto") == "filler"] == ["So"]
    assert any(c["text"] == "so I" for c in cuts)


def test_a_filler_is_not_cut_if_the_clip_would_be_shorter_than_three_seconds():
    job = _job(end=102.5)  # 2.5 s clip: nothing may be cut
    client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["so"]})
    assert _cuts(job["id"]) == []


def test_a_busy_job_refuses_the_change():
    job = _job(status="running")
    r = client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["so"]})
    assert r.status_code == 409


def test_the_list_is_checked_for_length():
    job = _job()
    r = client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["x" * 31]})
    assert r.status_code == 422
    r = client.post(f"/api/jobs/{job['id']}/clips/1/fillers", json={"on": True, "words": ["w"] * 21})
    assert r.status_code == 422


def test_words_match_regardless_of_case_and_punctuation():
    assert trims._plain("So,") == "so"
    assert trims._plain("Ain't!") == "ain't"
