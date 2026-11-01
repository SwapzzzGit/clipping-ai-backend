"""TikTok via Login Kit (OAuth v2) + Content Posting API (direct post, or upload to the inbox as a draft).

Sources (developers.tiktok.com returned HTTP 503 on 2026-10-31; endpoints below come from the
pages listed here as indexed by search plus their long-standing v2 shape. Recheck when the site is up):
  Login Kit (web): https://developers.tiktok.com/doc/login-kit-web
  Login Kit (desktop, PKCE required, S256): https://developers.tiktok.com/doc/login-kit-desktop
  Tokens (POST /v2/oauth/token/, refresh may rotate the refresh token):
    https://developers.tiktok.com/doc/oauth-user-access-token-management
  Direct post (POST /v2/post/publish/video/init/, scope video.publish; unaudited clients may only
    post SELF_ONLY, max 5 posting users per 24 h): https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
  Inbox upload (POST /v2/post/publish/inbox/video/init/, scope video.upload):
    https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
  Chunking (5-64 MB chunks, last chunk up to 128 MB, files < 5 MB in one chunk, PUT with Content-Range):
    https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
  Creator info (privacy options, max duration): https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
  Status (POST /v2/post/publish/status/fetch/): https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
"""

from __future__ import annotations

import hashlib
import os
import secrets
import urllib.parse
from pathlib import Path

from publisher.connectors.base import Connector
from publisher.models import Account, AuthError, Limits, PostSpec, PublishError, PublishResult, TokenSet

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = ["user.info.basic", "user.info.profile", "video.publish", "video.upload"]
MB = 1024 * 1024
MIN_CHUNK, MAX_CHUNK = 5 * MB, 64 * MB
PRIVACY = {"public": "PUBLIC_TO_EVERYONE", "friends": "MUTUAL_FOLLOW_FRIENDS",
           "followers": "FOLLOWER_OF_CREATOR", "private": "SELF_ONLY"}


def chunk_plan(size: int, chunk: int = 10 * MB) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per the media transfer guide; the last chunk absorbs the rest."""
    if size < MIN_CHUNK:
        return size, 1
    chunk = max(MIN_CHUNK, min(MAX_CHUNK, chunk, size))
    return chunk, max(1, size // chunk)


class TikTokConnector(Connector):
    name = "tiktok"
    label = "TikTok"
    client_id_env = ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_ID")
    client_secret_env = ("TIKTOK_CLIENT_SECRET",)
    max_concurrency = 2
    limits = Limits(
        max_duration_sec=600, min_duration_sec=3, title_max=None, caption_max=2200, hashtag_max=10,
        privacy_options=["public", "followers", "friends", "private"], private_only_unaudited=True,
        supports_resumable=True, max_file_mb=4096,
        notes=["Until the app passes TikTok's Content Posting audit, posts are SELF_ONLY (private) and at "
               "most 5 users can post per 24 h; set TIKTOK_AUDITED=1 after approval.",
               "The creator's own max duration (creator_info) can be shorter than 10 minutes.",
               "post_mode 'inbox' uploads a draft the creator finishes in the TikTok app."])

    @property
    def desktop(self) -> bool:
        """Desktop apps use PKCE and may redirect to http://localhost; web apps need an https redirect."""
        return os.environ.get("TIKTOK_APP_TYPE", "web").lower() == "desktop"

    @property
    def https_redirect(self) -> bool:  # type: ignore[override]
        return not self.desktop

    @property
    def supports_pkce(self) -> bool:  # type: ignore[override]
        return self.desktop

    @property
    def audited(self) -> bool:
        return os.environ.get("TIKTOK_AUDITED", "").lower() in ("1", "true", "yes")

    def make_pkce(self):
        # TikTok's desktop guide hex-encodes the SHA-256 of the verifier (not base64url).
        verifier = secrets.token_urlsafe(48)[:64]
        return verifier, hashlib.sha256(verifier.encode()).hexdigest()

    def auth_url(self, state, code_challenge=None, login_hint=None):
        self.require_configured()
        q = {"client_key": self.client_id, "scope": ",".join(SCOPES), "response_type": "code",
             "redirect_uri": self.redirect_uri(), "state": state}
        if code_challenge:
            q |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"

    def _token(self, data: dict, what: str) -> TokenSet:
        resp = self._request("POST", f"{API}/oauth/token/", what, data={
            "client_key": self.client_id, "client_secret": self.client_secret, **data},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        t = resp.json()
        if t.get("error") or "access_token" not in t:
            msg = f"TikTok {what}: {t.get('error')}: {t.get('error_description', '')}"
            if t.get("error") in ("invalid_grant", "access_denied", "invalid_request"):
                raise AuthError(msg)
            raise PublishError(msg, retryable=True)
        return TokenSet(access_token=t["access_token"], refresh_token=t.get("refresh_token"),
                        expires_at=TokenSet.expiry(t.get("expires_in")),
                        refresh_expires_at=TokenSet.expiry(t.get("refresh_expires_in")),
                        scopes=(t.get("scope") or "").split(","), extra={"open_id": t.get("open_id")})

    def exchange_code(self, code, code_verifier=None):
        self.require_configured()
        data = {"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect_uri()}
        if code_verifier:
            data["code_verifier"] = code_verifier
        return self._token(data, "code exchange")

    def refresh(self, account):
        if not account.tokens.refresh_token:
            raise AuthError("TikTok: no refresh token; reconnect")
        return self._token({"grant_type": "refresh_token", "refresh_token": account.tokens.refresh_token},
                           "refresh")

    def revoke(self, account):
        try:
            self.http.post(f"{API}/oauth/revoke/", data={"client_key": self.client_id,
                                                        "client_secret": self.client_secret,
                                                        "token": account.tokens.access_token})
        except Exception:
            pass

    # -- API helpers ------------------------------------------------------------------------

    def _api(self, account: Account, path: str, what: str, body: dict | None = None, method: str = "POST",
             params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {account.tokens.access_token}",
                   "Content-Type": "application/json; charset=UTF-8"}
        r = self._request(method, f"{API}{path}", what, headers=headers, json=body, params=params).json()
        err = r.get("error") or {}
        code = err.get("code", "ok")
        if code != "ok":
            msg = f"TikTok {what}: {code}: {err.get('message', '')}"
            if code in ("access_token_invalid", "scope_not_authorized", "token_expired"):
                raise AuthError(msg)
            if code in ("rate_limit_exceeded", "spam_risk_too_many_pending_share", "internal_error"):
                raise PublishError(msg, retryable=True, retry_after=60)
            raise PublishError(msg)
        return r.get("data") or {}

    def profile(self, account):
        d = self._api(account, "/user/info/", "user info", method="GET",
                      params={"fields": "open_id,union_id,avatar_url,display_name,username"})
        u = d.get("user", {})
        return {"remote_id": u.get("open_id") or account.tokens.extra.get("open_id"),
                "display_name": u.get("display_name", ""), "username": u.get("username", ""),
                "avatar_url": u.get("avatar_url")}

    def creator_info(self, account) -> dict:
        return self._api(account, "/post/publish/creator_info/query/", "creator info", body={})

    # -- upload --------------------------------------------------------------------------

    def privacy_level(self, post: PostSpec, options: list[str]) -> tuple[str, str]:
        wanted = PRIVACY.get(post.privacy, "SELF_ONLY")
        if not self.audited:
            return "SELF_ONLY", "app not audited by TikTok yet: posted as private (SELF_ONLY)"
        if options and wanted not in options:
            fallback = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
            return fallback, f"{wanted} not allowed for this creator; used {fallback}"
        return wanted, ""

    def upload(self, account, video_path: Path, post, ctx):
        size = video_path.stat().st_size
        mode = post.options.get("post_mode") or account.options.get("post_mode") or "direct"
        note = ""
        if not ctx.state.get("upload_url"):
            chunk, count = chunk_plan(size)
            source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
            if mode == "inbox":
                d = self._api(account, "/post/publish/inbox/video/init/", "inbox init", body={"source_info": source})
            else:
                info = self.creator_info(account)
                max_dur = info.get("max_video_post_duration_sec")
                dur = post.options.get("duration_sec")
                if max_dur and dur and dur > max_dur:
                    raise PublishError(f"Clip is {dur:.0f}s; this TikTok account allows {max_dur}s")
                level, note = self.privacy_level(post, info.get("privacy_level_options") or [])
                post_info = {
                    "title": post.caption_with_tags()[:2200], "privacy_level": level,
                    "disable_comment": bool(post.options.get("disable_comment", info.get("comment_disabled", False))),
                    "disable_duet": bool(post.options.get("disable_duet", info.get("duet_disabled", False))),
                    "disable_stitch": bool(post.options.get("disable_stitch", info.get("stitch_disabled", False))),
                    "video_cover_timestamp_ms": int(post.options.get("cover_ms", 1000)),
                    "brand_content_toggle": bool(post.options.get("brand_content", False)),
                    "brand_organic_toggle": bool(post.options.get("brand_organic", False)),
                    "is_aigc": bool(post.options.get("is_aigc", False)),
                }
                d = self._api(account, "/post/publish/video/init/", "direct post init",
                              body={"post_info": post_info, "source_info": source})
            ctx.state.update(publish_id=d["publish_id"], upload_url=d["upload_url"], chunk=chunk,
                             count=count, next_chunk=0, mode=mode, note=note,
                             username=account.username)
            ctx.save()
        s = ctx.state
        with open(video_path, "rb") as f:
            for i in range(s["next_chunk"], s["count"]):
                if ctx.cancelled():
                    raise PublishError("cancelled")
                start = i * s["chunk"]
                end = size - 1 if i == s["count"] - 1 else start + s["chunk"] - 1
                f.seek(start)
                data = f.read(end - start + 1)
                try:
                    resp = self.http.put(s["upload_url"], content=data, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {start}-{end}/{size}"})
                except Exception as e:
                    raise PublishError(f"TikTok chunk {i + 1} network error: {e}", retryable=True)
                if resp.status_code in (403, 404, 410):   # upload URL expired (valid ~1 h): re-init
                    for k in ("upload_url", "publish_id"):
                        s.pop(k, None)
                    ctx.save()
                    raise PublishError("TikTok upload URL expired", retryable=True)
                self._check(resp, f"chunk {i + 1}/{s['count']}")
                s["next_chunk"] = i + 1
                ctx.save()
                ctx.progress((i + 1) / s["count"])
        return PublishResult(status="processing", remote_id=s["publish_id"], message=s.get("note", ""),
                             state=dict(s))

    def status(self, account, result_state):
        d = self._api(account, "/post/publish/status/fetch/", "status",
                      body={"publish_id": result_state["publish_id"]})
        st = d.get("status")
        if st == "FAILED":
            raise PublishError(f"TikTok failed the post: {d.get('fail_reason', 'unknown')}")
        if st == "SEND_TO_USER_INBOX":
            return PublishResult(status="published", remote_id=result_state["publish_id"],
                                 message="Sent to the TikTok inbox: open TikTok to finish posting")
        if st == "PUBLISH_COMPLETE":
            ids = d.get("publicaly_available_post_id") or []   # sic: TikTok's field name
            user = result_state.get("username")
            url = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else None
            return PublishResult(status="published", remote_id=str(ids[0]) if ids else result_state["publish_id"],
                                 url=url, message=result_state.get("note", ""))
        return PublishResult(status="processing", remote_id=result_state["publish_id"], state=result_state)
