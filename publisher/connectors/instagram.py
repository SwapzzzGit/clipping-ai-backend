"""Instagram Reels via the Instagram API with Instagram Login (no Facebook Page needed).

Flow: container (POST /<IG_ID>/media, media_type=REELS, video_url=<public URL>) -> poll status_code
until FINISHED -> POST /<IG_ID>/media_publish. Instagram *pulls* the video from `video_url`, so the
queue hands this connector a temporary public URL (see publisher/public_url.py). Resumable uploads
from a local file (rupload.facebook.com) are only offered to Facebook Login for Business apps;
a `FacebookInstagramConnector` can be added for that path later.

Sources (checked 2026-10-31):
  Business Login (authorize, short-lived -> long-lived token, refresh, scopes
    instagram_business_basic + instagram_business_content_publish):
    https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
  Content publishing (REELS containers, status_code FINISHED/IN_PROGRESS/ERROR/EXPIRED/PUBLISHED,
    100 API posts per 24 h, professional accounts only):
    https://developers.facebook.com/docs/instagram-platform/content-publishing
  Reels limits (3 s - 15 min, 9:16 recommended, caption 2,200 chars / 30 hashtags):
    https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import httpx

from publisher.connectors.base import Connector
from publisher.models import AuthError, Limits, PublishError, PublishResult, TokenSet, parse_time

AUTH_URL = "https://www.instagram.com/oauth/authorize"
SHORT_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH = "https://graph.instagram.com"
SCOPES = ["instagram_business_basic", "instagram_business_content_publish"]


class InstagramConnector(Connector):
    name = "instagram_login"
    label = "Instagram Reels (Instagram Login, apps made before 2026)"
    client_id_env = ("INSTAGRAM_CLIENT_ID", "INSTAGRAM_APP_ID")
    client_secret_env = ("INSTAGRAM_CLIENT_SECRET", "INSTAGRAM_APP_SECRET")
    refresh_margin_sec = 7 * 86400      # long-lived tokens last 60 days; refresh a week early
    max_concurrency = 2
    https_redirect = True
    poll_interval_sec = 20   # the docs suggest polling about once a minute, for up to 5 minutes
    max_processing_sec = 1800
    limits = Limits(
        max_duration_sec=900, min_duration_sec=3, title_max=None, caption_max=2200, hashtag_max=30,
        privacy_options=["public"], needs_public_url=True, daily_post_limit=100,
        notes=["Requires an Instagram professional (Business or Creator) account.",
               "Instagram downloads the video from a public URL: start the free public link on the Accounts "
               "page (or set PUBLISHER_PUBLIC_BASE_URL / plug in another public URL provider).",
               "100 API-published posts per account per rolling 24 h."])

    @property
    def version(self) -> str:
        return os.environ.get("INSTAGRAM_GRAPH_VERSION", "v26.0")

    def auth_url(self, state, code_challenge=None, login_hint=None):
        self.require_configured()
        q = {"client_id": self.client_id, "redirect_uri": self.redirect_uri(), "response_type": "code",
             "scope": ",".join(SCOPES), "state": state, "enable_fb_login": "0", "force_reauth": "true"}
        return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            if err.get("code") == 190 or err.get("type") == "OAuthException" and err.get("code") in (102, 190):
                raise AuthError(f"Instagram {what}: token invalid ({err.get('message', '')})")
            if err.get("is_transient") or err.get("code") in (1, 2, 4, 17, 32, 613):
                raise PublishError(f"Instagram {what}: {err.get('message', resp.text[:300])}", retryable=True,
                                   retry_after=60 if err.get("code") in (4, 17, 32, 613) else None)
        return super()._check(resp, what)

    def exchange_code(self, code, code_verifier=None):
        self.require_configured()
        code = code.removesuffix("#_")
        r = self._request("POST", SHORT_TOKEN_URL, "code exchange", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "authorization_code", "redirect_uri": self.redirect_uri(), "code": code}).json()
        short = (r.get("data") or [r])[0]
        long = self._request("GET", f"{GRAPH}/access_token", "long-lived exchange", params={
            "grant_type": "ig_exchange_token", "client_secret": self.client_secret,
            "access_token": short["access_token"]}).json()
        perms = short.get("permissions") or ""
        return TokenSet(access_token=long["access_token"], expires_at=TokenSet.expiry(long.get("expires_in")),
                        scopes=perms.split(",") if isinstance(perms, str) else list(perms),
                        extra={"user_id": str(short.get("user_id", ""))})

    def should_refresh(self, tokens, now_ts):
        exp = parse_time(tokens.expires_at)
        return bool(exp and exp.timestamp() - now_ts < self.refresh_margin_sec)

    def refresh(self, account):
        r = self._request("GET", f"{GRAPH}/refresh_access_token", "refresh", params={
            "grant_type": "ig_refresh_token", "access_token": account.tokens.access_token}).json()
        return TokenSet(access_token=r["access_token"], expires_at=TokenSet.expiry(r.get("expires_in")),
                        scopes=account.tokens.scopes, extra=account.tokens.extra)

    def _get(self, account, path, what, **params):
        return self._request("GET", f"{GRAPH}/{self.version}/{path}", what, params=params,
                             headers={"Authorization": f"Bearer {account.tokens.access_token}"}).json()

    def _post(self, account, path, what, data):
        return self._request("POST", f"{GRAPH}/{self.version}/{path}", what, data=data,
                             headers={"Authorization": f"Bearer {account.tokens.access_token}"}).json()

    def profile(self, account):
        me = self._get(account, "me", "profile", fields="user_id,username,name,profile_picture_url,account_type")
        if me.get("account_type") not in (None, "BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            raise PublishError("Instagram publishing needs a professional (Business or Creator) account; "
                               "switch the account type in the Instagram app and reconnect.")
        return {"remote_id": str(me.get("user_id") or me.get("id")), "display_name": me.get("name") or "",
                "username": me.get("username", ""), "avatar_url": me.get("profile_picture_url")}

    def upload(self, account, video_path: Path, post, ctx):
        ig = account.remote_id
        if not ctx.state.get("container_id"):
            if not ctx.public_url:
                raise PublishError("Instagram needs the video at a public URL; set PUBLISHER_PUBLIC_BASE_URL "
                                   "(see docs/publishing.md)")
            data = {"media_type": "REELS", "video_url": ctx.public_url(str(video_path)),
                    "caption": post.caption_with_tags()[:2200],
                    "share_to_feed": "true" if post.options.get("share_to_feed", True) else "false"}
            if "cover_ms" in post.options:
                data["thumb_offset"] = str(int(post.options["cover_ms"]))
            r = self._post(account, f"{ig}/media", "create container", data)
            ctx.state.update(container_id=r["id"], ig_id=ig)
            ctx.save()
        ctx.progress(1.0)
        return PublishResult(status="processing", message="Instagram is fetching and processing the video",
                             state=dict(ctx.state))

    def status(self, account, result_state):
        cid = result_state["container_id"]
        if not result_state.get("media_id"):
            st = self._get(account, cid, "container status", fields="status_code,status")
            code = st.get("status_code")
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram container {code}: {st.get('status', '')}")
            if code != "FINISHED" and code != "PUBLISHED":
                return PublishResult(status="processing", state=result_state)
            r = self._post(account, f"{result_state['ig_id']}/media_publish", "publish", {"creation_id": cid})
            result_state = {**result_state, "media_id": r["id"]}
        media_id = result_state["media_id"]
        try:
            link = self._get(account, media_id, "permalink", fields="permalink").get("permalink")
        except PublishError:
            link = None
        return PublishResult(status="published", remote_id=media_id, url=link)
