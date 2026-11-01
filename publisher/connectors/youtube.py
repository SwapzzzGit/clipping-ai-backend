"""YouTube Shorts via Google OAuth 2.0 + YouTube Data API v3 resumable upload.

Sources (checked 2026-10-31):
  OAuth web-server flow + PKCE: https://developers.google.com/identity/protocols/oauth2/web-server
  videos.insert (quota: 1 unit in the "Video Uploads" bucket, 100 calls/day; scopes youtube.upload /
    youtube / youtube.force-ssl; max 256 GB; video/* or application/octet-stream):
    https://developers.google.com/youtube/v3/docs/videos/insert
  Resumable protocol (Location session URI, 256 KB-multiple chunks, 308 + Range, resume via
    `Content-Range: bytes */N`, 404 = session expired):
    https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
  Shorts = vertical/square video up to 3 minutes; there is no API flag, so "#Shorts" goes in the
    title/description: https://support.google.com/youtube/answer/10059070
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from publisher.connectors.base import Connector
from publisher.models import Account, AuthError, Limits, PostSpec, PublishError, PublishResult, TokenSet

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
CHUNK = 32 * 256 * 1024   # 8 MiB, a multiple of 256 KiB as the protocol requires


class YouTubeConnector(Connector):
    name = "youtube"
    label = "YouTube Shorts"
    supports_pkce = True
    poll_interval_sec = 15
    client_id_env = ("YOUTUBE_CLIENT_ID", "GOOGLE_CLIENT_ID")
    client_secret_env = ("YOUTUBE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET")
    limits = Limits(
        max_duration_sec=180, title_max=100, caption_max=5000, hashtag_max=15, hashtags_in="description",
        required_hashtags=["#Shorts"], privacy_options=["public", "unlisted", "private"],
        supports_resumable=True, daily_post_limit=100,
        notes=["Each upload uses 1 unit of the separate 'Video Uploads' quota (100/day by default).",
               "OAuth clients left in 'Testing' publishing status get refresh tokens that expire after 7 days.",
               "Uploads may still default to private depending on the channel's own settings."])

    def auth_url(self, state, code_challenge=None, login_hint=None):
        self.require_configured()
        q = {"client_id": self.client_id, "redirect_uri": self.redirect_uri(), "response_type": "code",
             "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent select_account",
             "include_granted_scopes": "true", "state": state}
        if code_challenge:
            q |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        if login_hint:
            q["login_hint"] = login_hint
        return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"

    def _token(self, data: dict, what: str) -> dict:
        try:
            resp = self.http.post(TOKEN_URL, data={"client_id": self.client_id,
                                                   "client_secret": self.client_secret, **data})
        except Exception as e:
            raise PublishError(f"Google {what} network error: {e}", retryable=True)
        if resp.status_code == 400 and "invalid_grant" in resp.text:
            raise AuthError(f"Google {what}: grant revoked or expired; reconnect the channel")
        return self._check(resp, what).json()

    def exchange_code(self, code, code_verifier=None):
        self.require_configured()
        data = {"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect_uri()}
        if code_verifier:
            data["code_verifier"] = code_verifier
        t = self._token(data, "code exchange")
        return TokenSet(access_token=t["access_token"], refresh_token=t.get("refresh_token"),
                        expires_at=TokenSet.expiry(t.get("expires_in")), scopes=t.get("scope", "").split())

    def refresh(self, account):
        if not account.tokens.refresh_token:
            raise AuthError("Google returned no refresh token; reconnect the channel")
        t = self._token({"grant_type": "refresh_token", "refresh_token": account.tokens.refresh_token}, "refresh")
        return TokenSet(access_token=t["access_token"],
                        refresh_token=t.get("refresh_token") or account.tokens.refresh_token,
                        expires_at=TokenSet.expiry(t.get("expires_in")),
                        scopes=t.get("scope", "").split() or account.tokens.scopes)

    def revoke(self, account):
        try:
            self.http.post(REVOKE_URL, data={"token": account.tokens.refresh_token or account.tokens.access_token})
        except Exception:
            pass

    def _auth(self, account: Account) -> dict:
        return {"Authorization": f"Bearer {account.tokens.access_token}"}

    def profile(self, account):
        r = self._request("GET", f"{API}/channels", "channels.list", headers=self._auth(account),
                          params={"part": "snippet", "mine": "true"}).json()
        items = r.get("items") or []
        if not items:
            raise PublishError("This Google account has no YouTube channel yet; create one first.")
        ch = items[0]
        sn = ch.get("snippet", {})
        return {"remote_id": ch["id"], "display_name": sn.get("title", ""),
                "username": sn.get("customUrl", ""),
                "avatar_url": (sn.get("thumbnails", {}).get("default") or {}).get("url")}

    # -- upload --------------------------------------------------------------------------

    def video_resource(self, post: PostSpec) -> dict:
        tags = [t.lstrip("#") for t in post.hashtags if t.lower() != "#shorts"]
        return {
            "snippet": {"title": post.title[:100], "description": post.caption_with_tags()[:5000],
                        "tags": tags[:30], "categoryId": str(post.options.get("category_id", "22"))},
            "status": {"privacyStatus": post.privacy if post.privacy in self.limits.privacy_options else "private",
                       "selfDeclaredMadeForKids": bool(post.options.get("made_for_kids", False)),
                       "containsSyntheticMedia": bool(post.options.get("contains_synthetic_media", False))},
        }

    def upload(self, account, video_path: Path, post, ctx):
        size = video_path.stat().st_size
        offset = 0
        session = ctx.state.get("session_uri")
        if session:
            offset, done = self._query_offset(account, session, size)
            if done is not None:
                return done
            if offset is None:            # session expired: start over
                session = None
        if not session:
            r = self._request("POST", UPLOAD_URL, "upload init",
                              params={"uploadType": "resumable", "part": "snippet,status"},
                              headers={**self._auth(account), "Content-Type": "application/json; charset=UTF-8",
                                       "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                              content=json.dumps(self.video_resource(post)).encode())
            session = r.headers.get("location")
            if not session:
                raise PublishError("YouTube did not return an upload session URL", retryable=True)
            ctx.state["session_uri"] = session
            ctx.save()
            offset = 0
        with open(video_path, "rb") as f:
            while True:
                if ctx.cancelled():
                    raise PublishError("cancelled")
                f.seek(offset)
                chunk = f.read(CHUNK)
                last = offset + len(chunk) - 1
                try:
                    resp = self.http.put(session, content=chunk, headers={
                        **self._auth(account), "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{last}/{size}"})
                except Exception as e:
                    raise PublishError(f"YouTube upload network error at byte {offset}: {e}", retryable=True)
                if resp.status_code == 308:
                    offset = _range_end(resp.headers.get("range"))
                    ctx.progress(offset / size)
                    continue
                if resp.status_code == 404:
                    ctx.state.pop("session_uri", None)
                    ctx.save()
                    raise PublishError("YouTube upload session expired", retryable=True)
                self._check(resp, "upload")
                ctx.progress(1.0)
                return self._uploaded(resp.json())

    def _query_offset(self, account, session, size):
        """(next_offset, finished_result). next_offset None = session gone."""
        try:
            resp = self.http.put(session, headers={**self._auth(account), "Content-Length": "0",
                                                   "Content-Range": f"bytes */{size}"})
        except Exception as e:
            raise PublishError(f"YouTube resume check failed: {e}", retryable=True)
        if resp.status_code == 308:
            return _range_end(resp.headers.get("range")), None
        if resp.status_code in (200, 201):
            return size, self._uploaded(resp.json())
        if resp.status_code == 404:
            return None, None
        self._check(resp, "resume check")
        return None, None

    def _uploaded(self, video: dict) -> PublishResult:
        vid = video["id"]
        url = f"https://www.youtube.com/shorts/{vid}"
        return PublishResult(status="processing", remote_id=vid, url=url, state={"remote_id": vid, "url": url},
                             message="Uploaded; YouTube is processing")

    def status(self, account, result_state):
        vid = result_state["remote_id"]
        r = self._request("GET", f"{API}/videos", "videos.list", headers=self._auth(account),
                          params={"part": "status,processingDetails", "id": vid}).json()
        items = r.get("items") or []
        if not items:
            return PublishResult(status="processing", remote_id=vid, url=result_state["url"], state=result_state)
        st = items[0].get("status", {})
        up = st.get("uploadStatus")
        if up in ("failed", "rejected", "deleted"):
            reason = st.get("failureReason") or st.get("rejectionReason") or up
            raise PublishError(f"YouTube {up} the video: {reason}")
        if up == "processed":
            return PublishResult(status="published", remote_id=vid, url=result_state["url"],
                                 message=f"privacy: {st.get('privacyStatus')}")
        return PublishResult(status="processing", remote_id=vid, url=result_state["url"], state=result_state)


def _range_end(header: str | None) -> int:
    """`Range: bytes=0-999` -> 1000 (next byte to send); no header -> 0."""
    if not header or "-" not in header:
        return 0
    return int(header.split("-")[-1]) + 1
