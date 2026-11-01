"""Instagram Reels via the Instagram API with Unified Login (Meta's setup for apps created since 2026).

Users press Connect, log in with Instagram *or* Facebook, pick their business portfolio and the
Instagram account(s) to share, and approve. No Facebook Page is needed. One login can grant
several Instagram accounts; each becomes its own connected account here.

Flow (all on graph.facebook.com):
  login   https://www.facebook.com/<v>/dialog/oauth?client_id=<META_APP_ID>&config_id=<CONFIG_ID>
          &redirect_uri=..&response_type=code&override_default_response_type=true&state=..
  token   GET /oauth/access_token?client_id&client_secret&redirect_uri&code
          -> Business Integration System User access token (belongs to the client's business
             portfolio; never expires by default; dies if they remove the app)
  ids     GET /me/assigned_instagram_accounts
  post    POST /<IG_ID>/media media_type=REELS upload_type=resumable -> container id
          POST https://rupload.facebook.com/ig-api-upload/<v>/<container>  (file bytes; offset, file_size)
          GET /<container>?fields=status_code,video_status  ->  POST /<IG_ID>/media_publish
  The video goes up straight from this machine: no public video URL needed.

Sources (checked 2026-11-01):
  https://developers.facebook.com/documentation/instagram-platform/overview
  https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-unified-login/get-started
  https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-unified-login/authentication
  https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business
  https://developers.facebook.com/documentation/instagram-platform/content-publishing/resumable-uploads.md
"""

from __future__ import annotations

import hashlib
import hmac
import os
import urllib.parse
from pathlib import Path

import httpx

from publisher import config
from publisher.connectors.base import Connector
from publisher.models import Account, AuthError, Limits, PublishError, PublishResult, TokenSet

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
DIALOG = "https://www.facebook.com"


class InstagramUnifiedConnector(Connector):
    name = "instagram"
    label = "Instagram Reels"
    client_id_env = ("META_APP_ID", "INSTAGRAM_CLIENT_ID")
    client_secret_env = ("META_APP_SECRET", "INSTAGRAM_CLIENT_SECRET")
    config_id_env = ("INSTAGRAM_CONFIG_ID", "META_CONFIG_ID")
    https_redirect = True
    max_concurrency = 2
    poll_interval_sec = 15
    max_processing_sec = 1800
    limits = Limits(
        max_duration_sec=900, min_duration_sec=3, title_max=None, caption_max=2200, hashtag_max=30,
        privacy_options=["public"], supports_resumable=True, daily_post_limit=100,
        notes=["Requires an Instagram professional (Business or Creator) account in a business portfolio.",
               "Users log in with Instagram or Facebook and pick the account(s) to share; no Facebook Page needed.",
               "100 API-published posts per account per rolling 24 h."])
    # Extra non-secret app setting (also readable from INSTAGRAM_CONFIG_ID).
    extra_fields = {"config_id": "Configuration ID"}

    @property
    def version(self) -> str:
        return os.environ.get("META_GRAPH_VERSION", "v26.0")

    @property
    def config_id(self) -> str:
        return config.env(*self.config_id_env) or (self.app_creds.get_extra(self.name).get("config_id", "")
                                                   if self.app_creds else "")

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.config_id)

    def require_configured(self) -> None:
        if not self.configured():
            from publisher.models import NotConfigured

            raise NotConfigured("Instagram isn't switched on: set META_APP_ID, META_APP_SECRET and "
                                "INSTAGRAM_CONFIG_ID in the server's .env (docs/publishing.md).")

    # -- OAuth ------------------------------------------------------------------------------

    def auth_url(self, state, code_challenge=None, login_hint=None):
        self.require_configured()
        q = {"client_id": self.client_id, "config_id": self.config_id, "redirect_uri": self.redirect_uri(),
             "response_type": "code", "override_default_response_type": "true", "state": state}
        return f"{DIALOG}/{self.version}/dialog/oauth?{urllib.parse.urlencode(q)}"

    def exchange_code(self, code, code_verifier=None):
        self.require_configured()
        r = self._request("GET", f"{GRAPH}/{self.version}/oauth/access_token", "code exchange", params={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri(), "code": code}).json()
        if "access_token" not in r:
            raise AuthError(f"Meta returned no access token: {str(r)[:200]}")
        tokens = TokenSet(access_token=r["access_token"], expires_at=TokenSet.expiry(r.get("expires_in")))
        try:
            me = self._get(tokens.access_token, "me", "business id", fields="client_business_id")
            tokens.extra["client_business_id"] = me.get("client_business_id")
        except PublishError:
            pass
        return tokens

    def should_refresh(self, tokens, now_ts):
        return False   # system-user tokens don't expire unless the business removes the app

    def refresh(self, account):
        return account.tokens

    def profiles(self, account: Account) -> list[dict]:
        r = self._get(account.tokens.access_token, "me/assigned_instagram_accounts", "Instagram accounts",
                      fields="id,username,name,profile_picture_url")
        out = []
        for ig in r.get("data") or []:
            if not ig.get("username"):
                try:
                    ig = {**ig, **self._get(account.tokens.access_token, ig["id"], "account",
                                            fields="username,name,profile_picture_url")}
                except PublishError:
                    pass
            out.append({"remote_id": str(ig["id"]), "display_name": ig.get("name") or ig.get("username") or "",
                        "username": ig.get("username", ""), "avatar_url": ig.get("profile_picture_url")})
        if not out:
            raise PublishError("No Instagram account was shared. Connect again and tick your Instagram account "
                               "(it must be a Business or Creator account in your business portfolio).")
        return out

    def profile(self, account):
        return self.profiles(account)[0]

    # -- HTTP -------------------------------------------------------------------------------

    def _proof(self, token: str) -> str:
        return hmac.new(self.client_secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            if err.get("code") in (190, 102) or err.get("type") == "OAuthException" and err.get("code") == 10:
                raise AuthError(f"Instagram {what}: access was removed or expired ({err.get('message', '')}); reconnect")
            if err.get("is_transient") or err.get("code") in (1, 2, 4, 17, 32, 613):
                raise PublishError(f"Instagram {what}: {err.get('message', resp.text[:300])}", retryable=True,
                                   retry_after=60 if err.get("code") in (4, 17, 32, 613) else None)
            if err.get("code") == 9 or err.get("error_subcode") == 2207042:
                raise PublishError("Instagram's 100 posts per 24 h limit is reached for this account", retryable=True,
                                   retry_after=3600)
        return super()._check(resp, what)

    def _get(self, token: str, path: str, what: str, **params):
        return self._request("GET", f"{GRAPH}/{self.version}/{path}", what,
                             params={**params, "appsecret_proof": self._proof(token)},
                             headers={"Authorization": f"Bearer {token}"}).json()

    def _post(self, token: str, path: str, what: str, data: dict):
        return self._request("POST", f"{GRAPH}/{self.version}/{path}", what,
                             data={**data, "appsecret_proof": self._proof(token)},
                             headers={"Authorization": f"Bearer {token}"}).json()

    # -- publishing -------------------------------------------------------------------------

    def upload(self, account, video_path: Path, post, ctx):
        token = account.tokens.access_token
        size = video_path.stat().st_size
        if not ctx.state.get("container_id"):
            data = {"media_type": "REELS", "upload_type": "resumable", "caption": post.caption_with_tags()[:2200],
                    "share_to_feed": "true" if post.options.get("share_to_feed", True) else "false"}
            if "cover_ms" in post.options:
                data["thumb_offset"] = str(int(post.options["cover_ms"]))
            r = self._post(token, f"{account.remote_id}/media", "create container", data)
            ctx.state.update(container_id=r["id"], ig_id=account.remote_id,
                             upload_uri=r.get("uri") or f"{RUPLOAD}/{self.version}/{r['id']}")
            ctx.save()
        offset = 0
        if ctx.state.get("started"):      # a retry: ask Instagram how much arrived
            st = self._get(token, ctx.state["container_id"], "upload status", fields="status_code,video_status")
            offset = int(((st.get("video_status") or {}).get("uploading_phase") or {}).get("bytes_transferred") or 0)
        if offset < size:
            ctx.state["started"] = True
            ctx.save()
            with open(video_path, "rb") as f:
                f.seek(offset)
                body = f.read()
            try:
                resp = self.http.post(ctx.state["upload_uri"], content=body, headers={
                    "Authorization": f"OAuth {token}", "offset": str(offset), "file_size": str(size)})
            except Exception as e:
                raise PublishError(f"Instagram upload network error: {e}", retryable=True)
            self._check(resp, "video upload")
        ctx.progress(1.0)
        return PublishResult(status="processing", message="Uploaded; Instagram is processing the video",
                             state={k: ctx.state[k] for k in ("container_id", "ig_id")})

    def status(self, account, result_state):
        token = account.tokens.access_token
        cid = result_state["container_id"]
        if not result_state.get("media_id"):
            st = self._get(token, cid, "container status", fields="status_code,status")
            code = st.get("status_code")
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram couldn't process the video ({code}): {st.get('status', '')}")
            if code not in ("FINISHED", "PUBLISHED"):
                return PublishResult(status="processing", state=result_state)
            r = self._post(token, f"{result_state['ig_id']}/media_publish", "publish", {"creation_id": cid})
            result_state = {**result_state, "media_id": r["id"]}
        try:
            link = self._get(token, result_state["media_id"], "permalink", fields="permalink").get("permalink")
        except PublishError:
            link = None
        return PublishResult(status="published", remote_id=result_state["media_id"], url=link)
