"""Real connectors against recorded-style HTTP fixtures (httpx.MockTransport). The response
shapes follow the platforms' documented examples; no request leaves the machine."""

import json
import urllib.parse

import httpx
import pytest

from publisher.connectors.instagram import InstagramConnector
from publisher.connectors.tiktok import MB, TikTokConnector, chunk_plan
from publisher.connectors.youtube import YouTubeConnector
from publisher.models import Account, AuthError, PostSpec, TokenSet, UploadContext


def client(handler, log):
    def wrapped(req: httpx.Request):
        log.append(req)
        return handler(req)
    return httpx.Client(transport=httpx.MockTransport(wrapped))


def acct(platform, remote="r1", username="me"):
    return Account(id=f"{platform}-1", platform=platform, remote_id=remote, username=username,
                   tokens=TokenSet(access_token="AT", refresh_token="RT", expires_at=TokenSet.expiry(3600)))


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"v" * (9 * 1024 * 1024 + 123))   # just over one 8 MiB YouTube chunk
    return p


# -- YouTube -------------------------------------------------------------------------------------

def test_youtube_auth_url_uses_pkce_and_offline():
    c = YouTubeConnector(client_id="cid", client_secret="sec")
    verifier, challenge = c.make_pkce()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(c.auth_url("st", challenge)).query)
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/youtube.upload" in q["scope"][0].split()
    assert q["redirect_uri"] == ["http://localhost:8000/api/publish/oauth/youtube/callback"]


def test_youtube_exchange_and_revoked_refresh():
    log = []

    def h(req):
        body = urllib.parse.parse_qs(req.content.decode())
        if body["grant_type"] == ["authorization_code"]:
            assert body["code_verifier"] == ["ver"]
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3599,
                                             "scope": "https://www.googleapis.com/auth/youtube.upload"})
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired"})
    c = YouTubeConnector(client(h, log), client_id="cid", client_secret="sec")
    t = c.exchange_code("CODE", "ver")
    assert t.refresh_token == "RT" and t.expires_at
    with pytest.raises(AuthError):
        c.refresh(acct("youtube"))


def test_youtube_resumable_upload_resumes_after_interruption(video):
    size = video.stat().st_size
    received = {"bytes": 0}
    log = []
    fail_once = {"done": False}

    def h(req):
        if req.url.path == "/upload/youtube/v3/videos":
            meta = json.loads(req.content)
            assert meta["snippet"]["title"] == "Pain needs attention #Shorts"
            assert meta["status"]["selfDeclaredMadeForKids"] is False
            assert req.headers["x-upload-content-length"] == str(size)
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/abc"})
        if req.url.path == "/upload/session/abc":
            cr = req.headers["content-range"]
            if cr.startswith("bytes */"):          # status query after the interruption
                return httpx.Response(308, headers={"Range": f"bytes=0-{received['bytes'] - 1}"})
            first, last = map(int, cr.split(" ")[1].split("/")[0].split("-"))
            assert first == received["bytes"]
            if last + 1 < size:
                received["bytes"] = last + 1
                if not fail_once["done"]:
                    fail_once["done"] = True
                    raise httpx.ConnectError("connection reset")
                return httpx.Response(308, headers={"Range": f"bytes=0-{last}"})
            received["bytes"] = size
            return httpx.Response(201, json={"id": "VID123", "status": {"uploadStatus": "uploaded"}})
        if req.url.path == "/youtube/v3/videos":
            return httpx.Response(200, json={"items": [{"id": "VID123", "status": {
                "uploadStatus": "processed", "privacyStatus": "public"}}]})
        raise AssertionError(req.url)

    c = YouTubeConnector(client(h, log), client_id="cid", client_secret="sec")
    state = {}
    ctx = UploadContext(state=state)
    post = PostSpec(title="Pain needs attention #Shorts", caption="quote", hashtags=["#Shorts", "#Pain"])
    with pytest.raises(Exception) as e:          # first attempt: network drops after chunk 1
        c.upload(acct("youtube"), video, post, ctx)
    assert getattr(e.value, "retryable", False) and state["session_uri"].endswith("/abc")
    res = c.upload(acct("youtube"), video, post, ctx)    # retry resumes from the session
    inits = [r for r in log if r.url.path == "/upload/youtube/v3/videos"]
    assert len(inits) == 1 and res.remote_id == "VID123" and res.status == "processing"
    assert res.url == "https://www.youtube.com/shorts/VID123"
    done = c.status(acct("youtube"), res.state)
    assert done.status == "published"


# -- TikTok --------------------------------------------------------------------------------------

def test_tiktok_chunk_plan():
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    assert chunk_plan(25 * MB) == (10 * MB, 2)          # last chunk carries the remaining 15 MB
    assert chunk_plan(7 * MB) == (7 * MB, 1)


def test_tiktok_direct_post_unaudited_is_private(tmp_path, monkeypatch):
    v = tmp_path / "c.mp4"
    v.write_bytes(b"t" * (12 * MB))
    log, puts = [], []

    def h(req):
        p = req.url.path
        if p == "/v2/post/publish/creator_info/query/":
            return httpx.Response(200, json={"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                                                      "max_video_post_duration_sec": 600, "comment_disabled": False},
                                             "error": {"code": "ok"}})
        if p == "/v2/post/publish/video/init/":
            body = json.loads(req.content)
            assert body["post_info"]["privacy_level"] == "SELF_ONLY"
            assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 12 * MB,
                                           "chunk_size": 10 * MB, "total_chunk_count": 1}
            return httpx.Response(200, json={"data": {"publish_id": "v_pub_1", "upload_url": "https://open-upload.tiktokapis.com/up/1"},
                                             "error": {"code": "ok"}})
        if req.url.host == "open-upload.tiktokapis.com":
            puts.append(req.headers["content-range"])
            return httpx.Response(201)
        if p == "/v2/post/publish/status/fetch/":
            return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE",
                                                      "publicaly_available_post_id": [7300000000000000001]},
                                             "error": {"code": "ok"}})
        raise AssertionError(req.url)

    c = TikTokConnector(client(h, log), client_id="ck", client_secret="cs")
    res = c.upload(acct("tiktok", username="creator"), v, PostSpec(caption="hi", hashtags=["#x"], privacy="public"),
                   UploadContext(state={}))
    assert puts == [f"bytes 0-{12 * MB - 1}/{12 * MB}"]
    assert "SELF_ONLY" in res.message
    done = c.status(acct("tiktok"), res.state)
    assert done.url == "https://www.tiktok.com/@creator/video/7300000000000000001"


def test_tiktok_desktop_pkce_is_hex(monkeypatch):
    monkeypatch.setenv("TIKTOK_APP_TYPE", "desktop")
    c = TikTokConnector(client_id="ck", client_secret="cs")
    assert c.supports_pkce
    v, ch = c.make_pkce()
    assert len(ch) == 64 and int(ch, 16) >= 0
    q = urllib.parse.parse_qs(urllib.parse.urlparse(c.auth_url("s", ch)).query)
    assert q["client_key"] == ["ck"] and q["scope"][0].split(",")[-1] == "video.upload"


def test_tiktok_token_error_is_auth_error():
    def h(req):
        return httpx.Response(200, json={"error": "invalid_grant", "error_description": "refresh token expired"})
    c = TikTokConnector(client(h, []), client_id="ck", client_secret="cs")
    with pytest.raises(AuthError):
        c.refresh(acct("tiktok"))


# -- Instagram -----------------------------------------------------------------------------------

def test_instagram_exchange_gets_long_lived_token():
    def h(req):
        if req.url.host == "api.instagram.com":
            assert urllib.parse.parse_qs(req.content.decode())["code"] == ["abc"]   # trailing #_ stripped
            return httpx.Response(200, json={"data": [{"access_token": "SHORT", "user_id": "1789",
                                                       "permissions": "instagram_business_basic,instagram_business_content_publish"}]})
        assert req.url.params["grant_type"] == "ig_exchange_token"
        return httpx.Response(200, json={"access_token": "LONG", "token_type": "bearer", "expires_in": 5183944})
    c = InstagramConnector(client(h, []), client_id="app", client_secret="sec")
    t = c.exchange_code("abc#_")
    assert t.access_token == "LONG" and t.extra["user_id"] == "1789" and t.refresh_token is None
    assert not c.should_refresh(t, __import__("time").time())


def test_instagram_reel_container_publish_flow(tmp_path):
    v = tmp_path / "c.mp4"
    v.write_bytes(b"i" * 1000)
    calls = []
    status = iter(["IN_PROGRESS", "FINISHED"])

    def h(req):
        calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer AT"
        p = req.url.path
        if p == "/v26.0/1789/media":
            form = urllib.parse.parse_qs(req.content.decode())
            assert form["media_type"] == ["REELS"] and form["video_url"][0].startswith("https://tunnel.example/")
            return httpx.Response(200, json={"id": "CONT1"})
        if p == "/v26.0/CONT1":
            return httpx.Response(200, json={"status_code": next(status), "id": "CONT1"})
        if p == "/v26.0/1789/media_publish":
            return httpx.Response(200, json={"id": "MEDIA9"})
        if p == "/v26.0/MEDIA9":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/XYZ/"})
        raise AssertionError(p)

    c = InstagramConnector(client(h, []), client_id="app", client_secret="sec")
    a = acct("instagram", remote="1789")
    res = c.upload(a, v, PostSpec(caption="cap", hashtags=["#a"]),
                   UploadContext(state={}, public_url=lambda p: "https://tunnel.example/m/tok/c.mp4"))
    assert res.status == "processing"
    assert c.status(a, res.state).status == "processing"
    done = c.status(a, res.state)
    assert done.status == "published" and done.url == "https://www.instagram.com/reel/XYZ/"


def test_instagram_requires_public_url(tmp_path):
    v = tmp_path / "c.mp4"
    v.write_bytes(b"i")
    c = InstagramConnector(client(lambda r: httpx.Response(500), []), client_id="a", client_secret="b")
    with pytest.raises(Exception, match="public URL"):
        c.upload(acct("instagram"), v, PostSpec(), UploadContext(state={}))


def test_instagram_expired_token_is_auth_error():
    def h(req):
        return httpx.Response(400, json={"error": {"message": "Error validating access token", "type": "OAuthException",
                                                   "code": 190}})
    c = InstagramConnector(client(h, []), client_id="a", client_secret="b")
    with pytest.raises(AuthError):
        c.profile(acct("instagram"))


# -- Instagram, Unified Login (new Meta apps) ---------------------------------------------------

from publisher.connectors.instagram_unified import InstagramUnifiedConnector


class _Creds:
    def get(self, name):
        return "", ""

    def get_extra(self, name):
        return {"config_id": "CFG42"}


def unified(handler, log=None):
    c = InstagramUnifiedConnector(client(handler, log if log is not None else []), client_id="APP", client_secret="SEC")
    c.app_creds = _Creds()
    return c


def test_unified_login_url_uses_config_id_not_scope():
    c = unified(lambda r: httpx.Response(500))
    u = urllib.parse.urlparse(c.auth_url("st"))
    q = urllib.parse.parse_qs(u.query)
    assert u.netloc == "www.facebook.com" and u.path == "/dialog/oauth"
    assert q["display"] == ["popup"] and q["prompt"] == ["login"]
    assert q["config_id"] == ["CFG42"] and "scope" not in q
    assert q["response_type"] == ["code"] and q["override_default_response_type"] == ["true"]


def test_unified_exchange_and_several_accounts_from_one_login():
    def h(req):
        p = req.url.path
        assert "appsecret_proof" in req.url.params or p.endswith("/oauth/access_token")
        if p.endswith("/oauth/access_token"):
            assert req.url.params["client_secret"] == "SEC" and req.url.params["code"] == "CODE"
            return httpx.Response(200, json={"access_token": "BISU", "token_type": "bearer"})
        if p.endswith("/me"):
            return httpx.Response(200, json={"client_business_id": "B1", "id": "sys"})
        if p.endswith("/me/assigned_instagram_accounts"):
            return httpx.Response(200, json={"data": [{"id": "1784", "username": "brand"}, {"id": "1785"}]})
        if p.endswith("/1785"):
            return httpx.Response(200, json={"username": "second", "name": "Second"})
        raise AssertionError(p)
    c = unified(h)
    t = c.exchange_code("CODE")
    assert t.access_token == "BISU" and t.expires_at is None and t.extra["client_business_id"] == "B1"
    assert not c.should_refresh(t, 0)
    profs = c.profiles(Account("n", "instagram", "", t))
    assert [p["username"] for p in profs] == ["brand", "second"] and profs[0]["remote_id"] == "1784"


def test_unified_resumable_upload_and_publish(tmp_path):
    v = tmp_path / "c.mp4"
    v.write_bytes(b"r" * 5000)
    seen = []

    def h(req):
        p = req.url.path
        if req.url.host == "rupload.facebook.com":
            assert req.headers["authorization"] == "OAuth AT" and req.headers["file_size"] == "5000"
            seen.append((req.headers["offset"], len(req.content)))
            return httpx.Response(200, json={"success": True})
        if p == "/v26.0/1784/media":
            form = urllib.parse.parse_qs(req.content.decode())
            assert form["media_type"] == ["REELS"] and form["upload_type"] == ["resumable"] and "video_url" not in form
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/C1"})
        if p == "/v26.0/C1":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if p == "/v26.0/1784/media_publish":
            return httpx.Response(200, json={"id": "M1"})
        if p == "/v26.0/M1":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        raise AssertionError(p)
    c = unified(h)
    a = acct("instagram", remote="1784")
    res = c.upload(a, v, PostSpec(caption="hi"), UploadContext(state={}))
    assert seen == [("0", 5000)] and res.status == "processing"
    done = c.status(a, res.state)
    assert done.status == "published" and done.url == "https://www.instagram.com/reel/abc/"


def test_unified_resume_continues_from_bytes_transferred(tmp_path):
    v = tmp_path / "c.mp4"
    v.write_bytes(b"r" * 5000)
    seen = []

    def h(req):
        if req.url.host == "rupload.facebook.com":
            seen.append((req.headers["offset"], len(req.content)))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"video_status": {"uploading_phase": {"bytes_transferred": 3000}}})
    c = unified(h)
    state = {"container_id": "C1", "ig_id": "1784", "upload_uri": "https://rupload.facebook.com/x/C1", "started": True}
    c.upload(acct("instagram", remote="1784"), v, PostSpec(), UploadContext(state=state))
    assert seen == [("3000", 2000)]


def test_unified_removed_app_is_auth_error():
    c = unified(lambda r: httpx.Response(400, json={"error": {"code": 190, "type": "OAuthException", "message": "x"}}))
    with pytest.raises(AuthError):
        c.profiles(acct("instagram"))
