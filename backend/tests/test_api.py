import json

from fastapi.testclient import TestClient

from app import claude_bridge
from app.main import app, settings, store, worker
from app.store import WORK_DIR
from clipper.util import write_json

# Local host + loopback client: the publisher tunnel guard 404s anything that looks external.
client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # no `with`: worker stays off


def make_transcript(n=60):
    segs, t = [], 0.0
    for i in range(n):
        words = []
        for tok in f"Sentence {i} tells a genuinely useful thing here.".split():
            words.append({"start": round(t, 2), "end": round(t + 0.4, 2), "text": tok})
            t += 0.4
        segs.append({"start_sec": words[0]["start"], "end_sec": words[-1]["end"], "text": " ".join(w["text"] for w in words), "words": words})
        t += 0.2
    return {"source": "auto", "lang": "en-orig", "requested_lang": "en", "has_word_timings": True,
            "segments": segs, "heatmap": [{"start_sec": 30, "end_sec": 40, "value": 0.9}]}


def test_create_job_validates_url_and_lengths():
    assert client.post("/api/jobs", json={"url": "https://vimeo.com/123"}).status_code == 422
    r = client.post("/api/jobs", json={"url": "https://youtu.be/rDdoUbCFn24", "min_sec": 60, "max_sec": 30})
    assert r.status_code == 422
    r = client.post("/api/jobs", json={"url": "https://youtu.be/rDdoUbCFn24", "mode": "api", "num_clips": 3, "rules": ""})
    assert r.status_code == 201
    job = r.json()
    assert job["status"] == "queued" and job["mode"] == "api" and job["options"]["num_clips"] == 3
    assert any(j["id"] == job["id"] for j in client.get("/api/jobs").json())


def test_auto_clip_count_is_the_default_and_stores_its_cap():
    r = client.post("/api/jobs", json={"url": "https://youtu.be/rDdoUbCFn24", "mode": "api", "rules": ""})
    assert r.status_code == 201
    assert r.json()["options"]["num_clips"] is None and r.json()["options"]["auto_max"] == 5


def test_music_choice_is_checked_when_the_job_is_created():
    url = "https://youtu.be/rDdoUbCFn24"
    assert client.post("/api/jobs", json={"url": url, "mode": "api", "rules": "", "music": "nope.mp3"}).status_code == 422
    r = client.post("/api/jobs", json={"url": url, "mode": "api", "rules": "", "music": "auto", "music_volume": 30})
    assert r.status_code == 201 and r.json()["options"]["music"] == "auto" and r.json()["options"]["music_volume"] == 30
    assert "tracks" in client.get("/api/music").json()


def test_branding_upload_defaults_and_job_snapshot():
    import cv2
    import numpy as np

    png = cv2.imencode(".png", np.zeros((30, 60, 4), np.uint8))[1].tobytes()
    r = client.post("/api/branding/upload?name=test-logo.png", content=png, headers={"content-type": "image/png"})
    assert r.status_code == 201 and r.json()["name"] == "test-logo.png"
    assert client.post("/api/branding/upload?name=notes.txt", content=b"x").status_code == 422
    assert client.put("/api/branding", json={"logo": "test-logo.png", "logo_position": "top-left"}).status_code == 200
    assert client.put("/api/branding", json={"logo_position": "middle"}).status_code == 422
    body = client.get("/api/branding").json()
    assert body["defaults"]["logo"] == "test-logo.png" and len(body["positions"]) == 9
    url = "https://youtu.be/rDdoUbCFn24"
    job = client.post("/api/jobs", json={"url": url, "mode": "api", "rules": ""}).json()
    assert job["options"]["branding"]["logo"] == "test-logo.png"
    plain = client.post("/api/jobs", json={"url": url, "mode": "api", "rules": "", "use_branding": False}).json()
    assert plain["options"]["branding"] is None
    assert client.delete("/api/branding/files/test-logo.png").status_code == 409  # still the logo
    client.put("/api/branding", json={"logo": None})
    assert client.delete("/api/branding/files/test-logo.png").status_code == 204
    assert client.get("/api/branding/files/test-logo.png").status_code == 404


def test_clip_branding_override_checks_its_input():
    job = client.post("/api/jobs", json={"url": "https://youtu.be/rDdoUbCFn24", "mode": "api", "rules": ""}).json()
    path = f"/api/jobs/{job['id']}/clips/1/branding"
    assert client.post(path, json={"logo_position": "middle-middle"}).status_code == 422
    assert client.post(path, json={"logo_position": "top-left"}).status_code == 409  # queued: wait until it is done


def test_settings_never_return_the_api_key():
    r = client.put("/api/settings", json={"llm_provider": "anthropic", "llm_api_key": "sk-secret-1234abcd"})
    body = r.json()
    assert body["has_api_key"] and body["api_key_hint"] == "…abcd"
    assert "sk-secret" not in json.dumps(body)
    assert settings.get()["llm_api_key"] == "sk-secret-1234abcd"
    body = client.put("/api/settings", json={"clear_api_key": True}).json()
    assert body["has_api_key"] is False


def test_claude_task_files_and_selection_roundtrip():
    job = store.create("https://youtu.be/AAAAAAAAAAA", "claude",
                       {"num_clips": 2, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "DON'T swear.", "captions": True})
    transcript = make_transcript()
    write_json(WORK_DIR / "AAAAAAAAAAA" / "transcript.json", transcript)
    store.update(job["id"], status="awaiting_claude", video={"id": "AAAAAAAAAAA"})
    cfg = worker.config_for(store.get(job["id"]))
    d = claude_bridge.write_task(store.get(job["id"]), {"title": "Test video", "duration": 300}, transcript, cfg, "DON'T swear.")
    task = (d / "task.md").read_text(encoding="utf-8")
    assert "**2** moments" in task and "DON'T swear." in task and "20-40 seconds" in task
    lines = (d / "transcript.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("[0.0] Sentence 0") and len(lines) == 60
    assert (d / "heatmap.txt").is_file()

    assert client.get("/api/claude/tasks").json()[0]["job_id"] == job["id"]
    seg = transcript["segments"]
    picks = {"clips": [
        {"start_sec": seg[10]["start_sec"] - 1, "end_sec": seg[18]["end_sec"], "title": "Ten", "hook_line": seg[10]["text"], "score": 8},
        {"start_sec": seg[12]["start_sec"], "end_sec": seg[16]["end_sec"], "title": "Overlaps"},
        {"start_sec": seg[30]["start_sec"], "end_sec": seg[38]["end_sec"], "title": "Thirty", "score": 7},
    ]}
    assert client.post(f"/api/jobs/{job['id']}/selection", json=picks).status_code == 200
    worker._collect_claude_selections()
    after = store.get(job["id"])
    assert after["status"] == "queued" and after["stage"] == "download"
    assert [c["title"] for c in after["picks"]] == ["Ten", "Thirty"]
    assert after["picks"][0]["start_sec"] == seg[10]["start_sec"]  # aligned to the hook, not 1 s early
    assert after["selection"]["by"] == "claude"


def test_bad_selection_is_rejected_and_job_keeps_waiting():
    job = store.create("https://youtu.be/AAAAAAAAAAA", "claude",
                       {"num_clips": 2, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True})
    store.update(job["id"], status="awaiting_claude", video={"id": "AAAAAAAAAAA"})
    d = claude_bridge.task_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "selection.json").write_text("{not json", encoding="utf-8")
    worker._collect_claude_selections()
    assert store.get(job["id"])["status"] == "awaiting_claude"
    assert not (d / "selection.json").exists() and list(d.glob("selection.rejected.*.json"))


def test_review_and_cancel():
    job = store.create("https://youtu.be/BBBBBBBBBBB", "api",
                       {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True})
    store.update(job["id"], status="done", clips=[{"id": 1, "review": {"approved": None, "notes": ""}}])
    r = client.patch(f"/api/jobs/{job['id']}/clips/1", json={"approved": True, "notes": "post Monday"})
    assert r.json()["clips"][0]["review"] == {"approved": True, "notes": "post Monday", "approved_by": "person"}
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/retry", json={"from_stage": "selection"}).json()["status"] == "queued"
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"


def test_find_claude_prefers_explicit_setting(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    assert claude_bridge.find_claude(str(exe)) == str(exe)
    assert claude_bridge.find_claude(str(tmp_path / "missing.exe")) is None


# -- review marks + re-render -------------------------------------------------------------

from clipper.candidates import snap_mark, words_text  # noqa: E402


def done_job_with_clip(vid, clip_extra=None, review=None):
    write_json(WORK_DIR / vid / "transcript.json", make_transcript())
    job = store.create(f"https://youtu.be/{vid}", "api",
                       {"num_clips": 1, "min_sec": 20, "max_sec": 40, "language": "en", "rules": "", "captions": True})
    clip = {"id": 1, "start_sec": 10.0, "end_sec": 40.0, "duration_sec": 30.45, "source_t0": 9.9,
            "review": review or {"approved": True, "notes": "keep", "marks": [], "trims": []}, **(clip_extra or {})}
    store.update(job["id"], status="done", video={"id": vid}, clips=[clip],
                 picks=[{"id": 1, "start_sec": 10.0, "end_sec": 40.0, "title": "T", "hook_line": "H", "text": "old"}])
    return job["id"]


def mark(jid, kind, t, clip=1):
    return client.post(f"/api/jobs/{jid}/clips/{clip}/marks", json={"kind": kind, "t": t})


def test_marks_are_snapped_and_stored():
    jid = done_job_with_clip("CCCCCCCCCC1")
    segs = make_transcript()["segments"]
    assert mark(jid, "start", 5.0).status_code == 200
    marks = mark(jid, "end", 20.0).json()["clips"][0]["review"]["marks"]
    assert [m["kind"] for m in marks] == ["start", "end"]
    assert marks[0]["source_sec"] == snap_mark(14.9, "start", segs, 10.0, 40.0)
    assert marks[1]["source_sec"] == snap_mark(29.9, "end", segs, 10.0, 40.0)
    starts = {w["start"] for s in segs for w in s["words"]}
    assert marks[0]["source_sec"] in starts and marks[0]["t"] == 5.0


def test_mark_on_clip_without_source_t0():
    jid = done_job_with_clip("CCCCCCCCCC2", review={"approved": None, "notes": ""})
    store.mutate(jid, lambda j: j["clips"][0].pop("source_t0"))
    r = mark(jid, "start", 5.0)
    assert r.status_code == 200
    segs = make_transcript()["segments"]
    assert r.json()["clips"][0]["review"]["marks"][0]["source_sec"] == snap_mark(14.9, "start", segs, 10.0, 40.0)


def test_mark_bounds():
    jid = done_job_with_clip("CCCCCCCCCC3")
    assert mark(jid, "start", 0).json()["clips"][0]["review"]["marks"][0]["source_sec"] == 10.2  # first word in the clip
    assert mark(jid, "end", 99).status_code == 422
    assert mark(jid, "end", 5, clip=7).status_code == 404


def test_clear_marks():
    jid = done_job_with_clip("CCCCCCCCCC4")
    mark(jid, "start", 5.0)
    r = client.delete(f"/api/jobs/{jid}/clips/1/marks")
    assert r.status_code == 200 and r.json()["clips"][0]["review"]["marks"] == []


def test_review_patch_keeps_marks():
    jid = done_job_with_clip("CCCCCCCCCC5")
    mark(jid, "start", 5.0)
    r = client.patch(f"/api/jobs/{jid}/clips/1", json={"approved": True, "notes": "x"})
    assert len(r.json()["clips"][0]["review"]["marks"]) == 1


def test_rerender_updates_pick_and_queues():
    jid = done_job_with_clip("CCCCCCCCCC6")
    m = [x for x in (mark(jid, "start", 5.0), mark(jid, "end", 20.0))][-1].json()["clips"][0]["review"]["marks"]
    r = client.post(f"/api/jobs/{jid}/clips/1/rerender")
    assert r.status_code == 200
    job = r.json()
    pick, clip = job["picks"][0], job["clips"][0]
    assert (pick["start_sec"], pick["end_sec"]) == (m[0]["source_sec"], m[1]["source_sec"])
    segs = make_transcript()["segments"]
    assert pick["text"] == words_text(segs, pick["start_sec"], pick["end_sec"]) and pick["text"].endswith("thing")
    assert pick["title"] == "T" and pick["hook_line"] == "H"
    assert clip["review"]["approved"] is None and clip["review"]["notes"] == "keep"
    assert clip["review"]["marks"] == [] and len(clip["review"]["trims"]) == 1
    assert clip["review"]["trims"][0]["from"] == [10.0, 40.0]
    assert clip["rerendering"] is True
    assert job["status"] == "queued" and job["stage"] == "download"
    assert "Re-rendering clip 1" in job["log"][-1]["msg"]


def test_rerender_with_end_mark_only():
    jid = done_job_with_clip("CCCCCCCCCC7")
    mark(jid, "end", 20.0)
    assert client.post(f"/api/jobs/{jid}/clips/1/rerender").json()["picks"][0]["start_sec"] == 10.0


def test_rerender_rejects_inverted_or_short():
    jid = done_job_with_clip("CCCCCCCCCC8")
    assert client.post(f"/api/jobs/{jid}/clips/1/rerender").status_code == 422      # no marks
    mark(jid, "start", 20.0)
    mark(jid, "end", 5.0)
    assert client.post(f"/api/jobs/{jid}/clips/1/rerender").status_code == 422      # inverted
    client.delete(f"/api/jobs/{jid}/clips/1/marks")
    mark(jid, "start", 5.0)
    mark(jid, "end", 6.5)
    assert client.post(f"/api/jobs/{jid}/clips/1/rerender").status_code == 422      # under 3 s
    assert store.get(jid)["status"] == "done"


def test_rerender_while_busy_is_409():
    jid = done_job_with_clip("CCCCCCCCCC9")
    mark(jid, "start", 5.0)
    store.update(jid, status="running")
    assert client.post(f"/api/jobs/{jid}/clips/1/rerender").status_code == 409


def test_review_edits_made_during_a_run_survive_it():
    jid = done_job_with_clip("DDDDDDDDDD1")
    store.update(jid, status="running")
    client.patch(f"/api/jobs/{jid}/clips/1", json={"approved": False, "notes": "edited mid-run"})
    manifest_clips = [{"id": 1, "status": "ok", "review": {"approved": None, "notes": "", "marks": [], "trims": []}}]
    worker._finish(jid, manifest_clips, total=1)
    job = store.get(jid)
    assert job["status"] == "done"
    assert job["clips"][0]["review"]["notes"] == "edited mid-run" and job["clips"][0]["review"]["approved"] is False


def test_marks_after_a_failed_rerender_cannot_lengthen_the_pick():
    jid = done_job_with_clip("DDDDDDDDDD2")
    store.mutate(jid, lambda j: j["picks"][0].update(start_sec=15.0, end_sec=30.0))  # pick moved, render failed
    m = mark(jid, "end", 29.0).json()["clips"][0]["review"]["marks"][0]
    assert m["source_sec"] <= 30.0
