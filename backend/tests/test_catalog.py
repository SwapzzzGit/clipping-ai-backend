"""Categories, batches, the approval gate and the posts tracker, end to end against the mock platform."""

import json
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app import catalog
from app.main import app, categories, store
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
    assert cat["id"] == "finance" and cat["hashtags"] == ["#money", "#investing"] and cat["approval"] == "manual"
    assert client.post("/api/categories", json={"name": "finance"}).status_code == 422  # duplicate, any case
    assert client.post("/api/categories", json={"approval": "auto"}).status_code == 422  # no name
    assert client.post("/api/categories", json={"name": "Bots", "approval": "auto"}).status_code == 422
    assert client.patch("/api/categories/finance", json={"approval": "auto"}).status_code == 422
    r = client.patch("/api/categories/finance", json={"hashtags": ["money"], "min_score": 8})
    assert r.json()["hashtags"] == ["#money"] and "min_score" not in r.json()
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


def test_old_auto_setting_is_migrated_to_manual(tmp_path):
    path = tmp_path / "categories.json"
    path.write_text(json.dumps([{"id": "fin", "name": "Fin", "approval": "auto", "min_score": 8}]))
    assert catalog.Categories(path).get("fin")["approval"] == "manual"
    assert json.loads(path.read_text())[0]["approval"] == "manual"
    assert (tmp_path / "categories.backup-before-auto-removal.json").is_file()


def test_only_a_person_approval_counts_for_publishing():
    assert catalog.human_approved({"review": {"approved": True, "approved_by": "person"}})
    assert not catalog.human_approved({"review": {"approved": True}})            # approved by no recorded person
    assert not catalog.human_approved({"review": {"approved": True, "approved_by": "auto"}})
    assert not catalog.human_approved({"review": {"approved": None}})
    assert not catalog.human_approved({})


def test_approving_posts_nothing_until_a_person_picks_accounts_for_the_clip():
    acct = connect_mock("motiv_main")
    other = connect_mock("motiv_second")
    cat = categories.create({"name": "Manual Motiv", "account_ids": [acct["id"]]})
    job = rendered_job(cat["id"])
    assert not store.get(job["id"]).get("publish")           # rendering alone posts nothing

    r = client.patch(f"/api/jobs/{job['id']}/clips/2", json={"approved": True, "notes": ""})
    assert r.status_code == 200
    assert r.json()["clips"][1]["review"]["approved_by"] == "person"
    assert not store.get(job["id"]).get("publish")           # approving alone posts nothing

    # The person picks the account for this clip: only that clip goes to only that account.
    url = f"/api/jobs/{job['id']}/publish"
    assert client.post(url, json={"clip_ids": [2], "account_ids": [other["id"]]}).status_code == 200
    pairs = store.get(job["id"])["publish"]["pairs"]
    assert [(p["clip_id"], p["account_id"]) for p in pairs] == [(2, other["id"])]

    assert client.post(url, json={"clip_ids": [2], "account_ids": [other["id"]]}).status_code == 409  # duplicate
    assert client.post(url, json={"clip_ids": [1], "account_ids": [other["id"]]}).status_code == 409  # not approved
    assert client.post(url, json={"clip_ids": [2]}).status_code == 422                               # no account


def test_publish_button_with_explicit_accounts_and_schedule():
    acct = connect_mock("solo")
    job = rendered_job(None)
    store.mutate(job["id"], lambda j: j["clips"][0]["review"].update(approved=True, approved_by="person"))
    assert client.post(f"/api/jobs/{job['id']}/publish", json={}).status_code == 422   # no category, no accounts
    r = client.post(f"/api/jobs/{job['id']}/publish",
                    json={"account_ids": [acct["id"]], "scheduled_at": "2030-01-01T09:00:00+00:00"})
    assert r.status_code == 200, r.text
    assert [t["status"] for t in r.json()["targets"]] == ["scheduled"]
