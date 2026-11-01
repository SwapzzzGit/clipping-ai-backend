"""Guided setup: UI-entered app credentials, the free public link (simulated), and the guard
that keeps everything but OAuth callbacks and clip links private when reached through it."""

import sys
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from publisher import config
from publisher.api import build_router, install_tunnel_guard
from publisher.connectors.instagram_unified import InstagramUnifiedConnector
from publisher.connectors.mock import MockConnector
from publisher.service import Publisher
from publisher.tunnel import URL_RE

FAKE_TUNNEL = [sys.executable, "-c",
               "import time; print('Connect to http://localhost.run/docs'); "
               "print('abc123def.lhr.life tunneled with tls termination, https://abc123def.lhr.life', flush=True); "
               "time.sleep(30)"]


@pytest.fixture
def app_env(data_dir, monkeypatch):
    monkeypatch.delenv("PUBLISHER_REDIRECT_BASE", raising=False)
    config.set_public_base(None)
    config.set_local_base(None)
    pub = Publisher(data_dir=data_dir, connectors={"instagram": InstagramUnifiedConnector(), "mock": MockConnector()},
                    ffmpeg="")
    pub.tunnel.command_override = FAKE_TUNNEL
    app = FastAPI()
    app.include_router(build_router(pub), prefix="/api/publish")
    install_tunnel_guard(app, pub)
    yield TestClient(app, base_url="http://localhost:8001"), pub
    pub.tunnel.stop(forget=False)
    config.set_public_base(None)
    config.set_local_base(None)


def test_url_regex_ignores_docs_links():
    assert URL_RE.search("see https://admin.localhost.run/ and https://x9.lhr.life").group(0) == "https://x9.lhr.life"
    assert URL_RE.search("tunneled with tls termination, https://ab12.lhr.life").group(0) == "https://ab12.lhr.life"


def test_credentials_saved_encrypted_and_used(app_env, data_dir):
    client, pub = app_env
    s = client.get("/api/publish/setup/instagram").json()
    assert s["platform"]["configured"] is False and s["credentials_source"] == ""
    s = client.put("/api/publish/setup/instagram/credentials",
                   json={"client_id": "1234567890", "client_secret": "supersecretvalue",
                         "extra": {"config_id": "555", "evil": "x"}}).json()
    assert s["platform"]["configured"] and s["credentials_source"] == "ui"
    assert s["credentials"] == {"client_id": "1234567890", "has_secret": True, "secret_hint": "…alue",
                                "extra": {"config_id": "555"}}
    assert "supersecretvalue" not in (data_dir / "app_credentials.json").read_text()
    # updating only the id keeps the secret
    client.put("/api/publish/setup/instagram/credentials", json={"client_id": "999999"})
    assert pub.connector("instagram").client_secret == "supersecretvalue"
    assert client.delete("/api/publish/setup/instagram/credentials").json()["platform"]["configured"] is False


def test_tunnel_drives_https_redirect_and_public_urls(app_env):
    client, pub = app_env
    client.put("/api/publish/setup/instagram/credentials", json={"client_id": "123", "client_secret": "s3cretvalue",
                                                                           "extra": {"config_id": "777"}})
    before = client.get("/api/publish/setup/instagram").json()
    assert before["redirect_uri"].startswith("http://localhost:8001/")     # learned from the request
    assert before["public_url_ready"] is False
    st = client.post("/api/publish/tunnel/start").json()
    assert st["state"] == "on" and st["url"] == "https://abc123def.lhr.life"
    after = client.get("/api/publish/setup/instagram").json()
    assert after["redirect_uri"] == "https://abc123def.lhr.life/api/publish/oauth/instagram/callback"
    assert after["public_url_ready"] and after["tunnel"]["autostart"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(
        client.get("/api/publish/oauth/instagram/start").json()["auth_url"]).query)
    assert q["redirect_uri"] == [after["redirect_uri"]] and q["client_id"] == ["123"] and q["config_id"] == ["777"]
    assert pub.url_provider.url_for(__file__).startswith("https://abc123def.lhr.life/api/publish/media/")
    assert after["extra_fields"] == {"config_id": "Configuration ID"}
    stopped = client.post("/api/publish/tunnel/stop").json()
    assert stopped["state"] == "off" and not stopped["autostart"] and pub.url_provider is None


def test_guard_only_exposes_callbacks_and_media(app_env, clip_source):
    client, pub = app_env
    ext = {"host": "abc123def.lhr.life", "x-forwarded-for": "203.0.113.9"}
    assert client.get("/api/publish/accounts", headers=ext).status_code == 200    # no tunnel: guard is off
    client.post("/api/publish/tunnel/start")
    assert client.get("/api/publish/accounts").status_code == 200                 # local: everything
    assert client.get("/api/publish/accounts", headers=ext).status_code == 404     # via the link: hidden
    assert client.get("/api/publish/setup/instagram", headers=ext).status_code == 404
    assert client.post("/api/publish/tunnel/stop", headers=ext).status_code == 404
    assert client.get("/api/publish/ping", headers=ext).json()["ok"] is True
    r = client.get("/api/publish/oauth/instagram/callback?code=x&state=bad", headers=ext)
    assert r.status_code == 400 and "expired" in r.text                          # reachable, state still enforced
    pub._tunnel_urls.base_url = "https://abc123def.lhr.life"
    pub.url_provider = pub._tunnel_urls
    path = urllib.parse.urlparse(pub.url_provider.url_for(str(clip_source / "01_clip.mp4"))).path
    assert client.get(path, headers=ext).status_code == 200
    # a localhost Host header with forwarding headers is still treated as external
    assert client.get("/api/publish/accounts", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 404


def test_connect_blocked_until_admin_setup_is_done(app_env):
    client, _ = app_env
    ig = lambda: next(p for p in client.get("/api/publish/platforms").json() if p["id"] == "instagram")
    assert ig()["connect_ready"] is False and ".env" in ig()["blocker"]
    client.put("/api/publish/setup/instagram/credentials", json={"client_id": "123", "client_secret": "s3cretvalue",
                                                                           "extra": {"config_id": "777"}})
    assert "https" in ig()["blocker"]                                   # keys alone: still needs the public address
    r = client.get("/api/publish/oauth/instagram/start")
    assert r.status_code == 409
    client.post("/api/publish/tunnel/start")
    assert ig()["connect_ready"] is True and ig()["blocker"] is None
    assert client.get("/api/publish/oauth/instagram/start").status_code == 200


def test_dotenv_loader_does_not_override_real_env(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nMETA_APP_ID=111\nexport INSTAGRAM_CONFIG_ID='222'\nMETA_APP_SECRET=\n", encoding="utf-8")
    monkeypatch.setenv("META_APP_ID", "from-env")
    config.load_dotenv(f)
    import os
    assert os.environ["META_APP_ID"] == "from-env" and os.environ["INSTAGRAM_CONFIG_ID"] == "222"
    assert "META_APP_SECRET" not in os.environ        # empty values are ignored
