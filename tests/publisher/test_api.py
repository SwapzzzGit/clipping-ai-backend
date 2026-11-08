import urllib.parse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from publisher.api import build_router
from publisher.connectors.mock import MockConnector
from publisher.connectors.youtube import YouTubeConnector
from publisher.public_url import TunnelProvider
from publisher.service import Publisher


def make_app(data_dir):
    prov = TunnelProvider("https://tunnel.example")
    pub = Publisher(data_dir=data_dir, connectors={"mock": MockConnector(), "youtube": YouTubeConnector()},
                    url_provider=prov, ffmpeg="")
    app = FastAPI()
    app.include_router(build_router(pub), prefix="/api/publish")
    return TestClient(app), pub, prov


def connect(client, handle):
    r = client.get("/api/publish/oauth/mock/start", params={"login_hint": handle})
    url = urllib.parse.urlparse(r.json()["auth_url"])
    assert url.path == "/api/publish/oauth/mock/callback"
    r = client.get(url.path + "?" + url.query)
    assert r.status_code == 200 and "Connected" in r.text
    return [a for a in client.get("/api/publish/accounts").json() if a["username"] == handle][0]


def test_platforms_and_unconfigured_start(data_dir):
    client, _, _ = make_app(data_dir)
    plats = {p["id"]: p for p in client.get("/api/publish/platforms").json()}
    assert plats["youtube"]["configured"] is False and plats["mock"]["configured"] is True
    assert plats["youtube"]["redirect_uri"].endswith("/api/publish/oauth/youtube/callback")
    r = client.get("/api/publish/oauth/youtube/start")
    assert r.status_code == 409 and "YOUTUBE_CLIENT_ID" in r.json()["detail"]


def test_oauth_flow_variants_job_and_status(data_dir, clip_source):
    client, pub, _ = make_app(data_dir)
    a, b = connect(client, "main"), connect(client, "second")
    assert "tokens" not in a
    r = client.get("/api/publish/oauth/mock/callback", params={"code": "mock-x", "state": "forged"})
    assert r.status_code == 400

    v = client.post("/api/publish/variants", json={"source": str(clip_source), "account_ids": [a["id"], b["id"]]})
    assert v.status_code == 200 and len(v.json()) == 4
    targets = [{"clip_id": x["clip_id"], "account_id": x["account_id"],
                "post": {"caption": "edited " + x["post"]["caption"]}} for x in v.json()]
    job = pub.create_job(str(clip_source), targets=targets)
    assert job["status"] == "queued" and all(t["post"]["caption"].startswith("edited") for t in job["targets"])
    pub.start()
    try:
        assert pub.wait_idle(20)
    finally:
        pub.stop()
    got = client.get(f"/api/publish/jobs/{job['id']}").json()
    assert got["status"] == "published" and all(t["url"] for t in got["targets"])
    assert client.get("/api/publish/jobs/nope").status_code == 404
    # Posts are created only through the backend's approve-then-publish route, never from here.
    assert client.post("/api/publish/jobs", json={}).status_code == 405

    client.patch(f"/api/publish/accounts/{a['id']}", json={"options": {"hashtags": ["#mine"]}})
    v2 = client.post("/api/publish/variants", json={"source": str(clip_source), "account_ids": [a["id"]],
                                                     "clip_ids": [1]}).json()
    assert "#Mine" in v2[0]["post"]["hashtags"] or "#mine" in v2[0]["post"]["hashtags"]
    assert client.delete(f"/api/publish/accounts/{b['id']}").status_code == 204
    assert len(client.get("/api/publish/accounts").json()) == 1


def test_media_endpoint_serves_only_registered_files(data_dir, clip_source):
    client, _, prov = make_app(data_dir)
    url = prov.url_for(str(clip_source / "01_clip.mp4"))
    path = urllib.parse.urlparse(url).path
    assert client.get(path).status_code == 200
    assert client.get(path.replace("01_clip", "02_clip")).status_code == 404
    assert client.get("/api/publish/media/badtoken/01_clip.mp4").status_code == 404


def test_bad_source_is_404(data_dir):
    client, _, _ = make_app(data_dir)
    acct = connect(client, "z")
    r = client.post("/api/publish/variants", json={"source": "missing", "account_ids": [acct["id"]]})
    assert r.status_code == 404
