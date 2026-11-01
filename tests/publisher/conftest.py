import json

import pytest

from publisher.accounts import AccountStore
from publisher.connectors.mock import MockConnector
from publisher.models import TokenSet


@pytest.fixture(autouse=True)
def publisher_env(tmp_path, monkeypatch):
    """Every test gets its own data dir and generated secret key; no real platform is reachable."""
    monkeypatch.setenv("PUBLISHER_DATA", str(tmp_path / "publish"))
    monkeypatch.delenv("CLIPPER_SECRET_KEY", raising=False)
    monkeypatch.setenv("PUBLISHER_MOCK_DELAY", "0.08")
    monkeypatch.setenv("PUBLISHER_REDIRECT_BASE", "http://localhost:8000")
    for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
                "INSTAGRAM_CLIENT_ID", "INSTAGRAM_CLIENT_SECRET", "TIKTOK_AUDITED", "TIKTOK_APP_TYPE",
                "PUBLISHER_PUBLIC_BASE_URL", "PUBLISHER_PUBLIC_URL_PROVIDER", "META_APP_ID", "META_APP_SECRET",
                "INSTAGRAM_CONFIG_ID", "META_CONFIG_ID", "PUBLISHER_TUNNEL", "NGROK_DOMAIN", "NGROK_AUTHTOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "publish"


@pytest.fixture
def clip_source(tmp_path):
    """A rendered-clips folder shaped like clipper's output/<video_id>/."""
    folder = tmp_path / "output" / "vid123"
    folder.mkdir(parents=True)
    clips = []
    for i, (title, dur) in enumerate([("Why physical pain requires attention", 37.8),
                                      ("The 10-minute rule to beat cravings", 52.0)], 1):
        name = f"0{i}_clip.mp4"
        (folder / name).write_bytes(b"\x00" * (300_000 + i))
        clips.append({"id": i, "status": "ok", "file": name, "title": title,
                      "hook_line": "The pain happens because we", "reason": "Counter-intuitive insight.",
                      "score": 8.5, "duration_sec": dur, "source_url": f"https://youtu.be/vid123?t={i * 100}",
                      "transcript": "The pain happens because we focus our attention on the pain. "
                                    "You can't have pain without attention. Do you notice that? " * 3})
    manifest = {"video": {"id": "vid123", "url": "https://youtu.be/vid123", "title": "Habit Expert Podcast",
                          "channel": "The Show"}, "clips": clips}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def connect_mock(store: AccountStore, handle: str) -> dict:
    c = MockConnector()
    tokens = c.exchange_code(f"mock-{handle}")
    from publisher.models import Account

    return store.upsert("mock", tokens, c.profile(Account("x", "mock", "", tokens)))


@pytest.fixture
def expired_tokens():
    return TokenSet(access_token="old", refresh_token="rt", expires_at=TokenSet.expiry(10))
