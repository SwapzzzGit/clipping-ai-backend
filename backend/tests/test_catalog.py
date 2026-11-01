"""Categories, batches, autopublish and the posts tracker, end to end against the mock platform."""

import json
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app import catalog
from app.main import app, categories, publisher, store
from app.store import OUTPUT_DIR

# Local host + loopback client: the publisher tunnel guard 404s anything that looks external.
client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))  # worker stays off


def connect_mock(handle: str) -> dict:
    """Run the real OAuth start -> callback round trip against the mock platform."""
    url = client.get("/api/publish/oauth/mock/start", params={"login_hint": handle}).json()["auth_url"]
    u = urlparse(url)
    r = client.get(u.path + "?" + u.query)
    assert r.status_code == 200, r.text
    return next(a for a in client.get("/api/publish/accounts").json() if a["username"] == handle)


def rendered_job(category_id: str | None, scores=(8.5, 6.0, 9.1)) -> dict:
    """A finished job with a manifest on disk, as the worker leaves it."""
    job = store.create("https://youtu.be/CCCCCCCCCCC", "api",
                       {"num_clips": 3, "min_sec": 20, "max_sec": 60, "language": "en", "rules": "", "captions": True},
                       category_id=category_id)
    folder = OUTPUT_DIR / job["id"]
    folder.mkdir(parents=True)
    clips = []
    for i, score in enumerate(scores, 1):
        (folder / f"0{i}_c.mp4").write_bytes(b"\x00" * 200_000)
        clips.append({"id": i, "status": "ok", "file": f"0{i}_c.mp4", "title": f"Clip {i}", "hook_line": "Hook.",
                      "reason": "r", "score": score, "score_breakdown": {}, "heatmap": 0, "duration_sec": 30,
                      "start_sec": i * 100, "end_sec": i * 100 + 30, "source_url": f"https://youtu.be/CCCCCCCCCCC?t={i}",
                      "transcript": "Some words about money and habits.",
                      "review": {"approved": None, "notes": "", "marks": [], "trims": []}})
    (folder / "manifest.json").write_text(json.dumps({"video": {"id": "CCCCCCCCCCC", "title": "Pod"}, "clips": clips}))
    store.update(job["id"], status="done", stage="done", clips=clips, video={"id": "CCCCCCCCCCC", "title": "Pod"})
    return store.get(job["id"])


def test_categories_crud_and_validation():
    r = client.post("/api/categories", json={"name": "Finance", "hashtags": ["money", "#investing"]})
    assert r.status_code == 201
    cat = r.json()
    assert cat["id"] == "finance" and cat["hashtags"] == ["#money", "#investing"]
    assert client.post("/api/categories", json={"name": "finance"}).status_code == 422  # duplicate, any case
    assert client.post("/api/categories", json={"approval": "auto"}).status_code == 422  # no name
    r = client.patch("/api/categories/finance", json={"approval": "auto", "min_score": 8})
    assert r.json()["approval"] == "auto" and r.json()["min_score"] == 8
    assert client.patch("/api/categories/nope", json={"name": "x"}).status_code == 404
    assert any(c["id"] == "finance" for c in client.get("/api/categories").json())


def test_batch_text_assigns_categories_and_reports_bad_lines():
    categories.create({"name": "Motivation"})
    text = """# url, category
https://www.youtube.com/watch?v=AAAAAAAAAAA, motivation
https://youtu.be/BBBBBBBBBBB\tMotivation
https://vimeo.com/1, motivation
https://youtu.be/DDDDDDDDDDD, cooking
https://youtu.be/EEEEEEEEEEE
"""
    r = client.post("/api/batches", json={"text": text, "default_category": "motivation", "num_clips": 2})
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["jobs"]) == 3
    assert any("not a YouTube" in e for e in body["errors"]) and any("cooking" in e for e in body["errors"])
    jobs = [store.get(j) for j in body["jobs"]]
    assert {j["category_id"] for j in jobs} == {"motivation"}
    assert {j["batch_id"] for j in jobs} == {body["batch_id"]}
    assert jobs[0]["options"]["num_clips"] == 2
    assert any(b["id"] == body["batch_id"] and b["jobs"] == 3 for b in client.get("/api/batches").json())


def test_inbox_file_becomes_a_batch(tmp_path):
    categories.create({"name": "Inbox Cat"})
    catalog.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (catalog.INBOX_DIR / "urls.csv").write_text("url,category\nhttps://youtu.be/FFFFFFFFFFF,inbox cat\n")
    from app.main import _create_batch

    assert catalog.ingest_inbox(_create_batch) == ["urls.csv"]
    assert not (catalog.INBOX_DIR / "urls.csv").exists()
    assert any(j["url"].endswith("FFFFFFFFFFF") and j["category_id"] == "inbox-cat" for j in store.list())


def test_auto_category_publishes_top_clips_to_every_account_once():
    a1, a2 = connect_mock("fin_one"), connect_mock("fin_two")
    other = connect_mock("not_in_category")
    cat = categories.create({"name": "Auto Fin", "approval": "auto", "min_score": 8, "max_clips_per_video": 5,
                             "account_ids": [a1["id"], a2["id"]], "hashtags": ["#finance"]})
    job = rendered_job(cat["id"], scores=(8.5, 6.0, 9.1))

    from app.main import autopub
    publisher.start()
    try:
        autopub.on_rendered(job["id"])
        after = store.get(job["id"])
        approved = sorted(c["id"] for c in after["clips"] if c["review"].get("approved"))
        assert approved == [1, 3]                          # 6.0 is below min_score
        pairs = {(p["clip_id"], p["account_id"]) for p in after["publish"]["pairs"]}
        assert pairs == {(c, a) for c in (1, 3) for a in (a1["id"], a2["id"])}
        assert all(a != other["id"] for _, a in pairs)
        assert publisher.wait_idle(10)

        # Running the hook again (e.g. after a restart) must not post anything twice.
        autopub.on_rendered(job["id"])
        assert len(store.get(job["id"])["publish"]["jobs"]) == 1

        pj = publisher.jobs.get(after["publish"]["jobs"][0])
        assert {t["status"] for t in pj["targets"]} == {"published"}
        assert all("#finance" in t["post"]["hashtags"] for t in pj["targets"])

        rows = [r for r in client.get("/api/posts").json()["rows"] if r["job_id"] == job["id"]]
        status = {(r["clip_id"], r["account_id"]): r["status"] for r in rows}
        assert status[(1, a1["id"])] == "published" and status[(3, a2["id"])] == "published"
        assert status[(2, a1["id"])] == "awaiting_review"   # not approved, so tracked as not posted yet
    finally:
        publisher.stop()


def test_manual_category_publishes_only_on_approval():
    acct = connect_mock("motiv_main")
    cat = categories.create({"name": "Manual Motiv", "approval": "manual", "account_ids": [acct["id"]]})
    job = rendered_job(cat["id"])
    from app.main import autopub

    autopub.on_rendered(job["id"])
    assert not store.get(job["id"]).get("publish")           # nothing posts without a person

    r = client.patch(f"/api/jobs/{job['id']}/clips/2", json={"approved": True, "notes": ""})
    assert r.status_code == 200
    pairs = store.get(job["id"])["publish"]["pairs"]
    assert [(p["clip_id"], p["account_id"]) for p in pairs] == [(2, acct["id"])]

    # Publishing the same clip again through the button is refused as a duplicate.
    assert client.post(f"/api/jobs/{job['id']}/publish", json={}).status_code == 409


def test_publish_button_with_explicit_accounts_and_schedule():
    acct = connect_mock("solo")
    job = rendered_job(None)
    store.mutate(job["id"], lambda j: j["clips"][0]["review"].update(approved=True))
    assert client.post(f"/api/jobs/{job['id']}/publish", json={}).status_code == 422   # no category, no accounts
    r = client.post(f"/api/jobs/{job['id']}/publish",
                    json={"account_ids": [acct["id"]], "scheduled_at": "2030-01-01T09:00:00+00:00"})
    assert r.status_code == 200, r.text
    assert [t["status"] for t in r.json()["targets"]] == ["scheduled"]
