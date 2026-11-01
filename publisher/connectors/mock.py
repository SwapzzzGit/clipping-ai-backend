"""Offline connector for tests and demos. Makes no network calls.

OAuth "consent" redirects straight back to our own callback. Uploads copy nothing: they record the
post as JSON under <data_dir>/mock_posts/<account>/ and report a fake URL. Failure injection via
post.options: {"mock_fail_times": 2} fails the first two attempts with a retryable error,
{"mock_fail": "permanent"} fails for good, {"mock_auth_fail": true} acts like a revoked token.
"""

from __future__ import annotations

import os
import secrets
import time
import urllib.parse
from pathlib import Path

from publisher import config
from publisher.connectors.base import Connector
from publisher.models import Account, AuthError, Limits, PostSpec, PublishError, PublishResult, TokenSet
from clipper.util import write_json


class MockConnector(Connector):
    name = "mock"
    label = "Mock (offline)"
    supports_pkce = True
    max_concurrency = 4
    poll_interval_sec = 0.05
    limits = Limits(max_duration_sec=180, title_max=100, caption_max=2200, hashtag_max=10,
                    privacy_options=["public", "private", "unlisted"], supports_resumable=True,
                    notes=["Offline test platform; nothing leaves this machine."])

    def configured(self) -> bool:
        return True

    @property
    def delay(self) -> float:
        return float(os.environ.get("PUBLISHER_MOCK_DELAY", "0.2"))

    def auth_url(self, state, code_challenge=None, login_hint=None):
        code = "mock-" + (login_hint or secrets.token_hex(3))
        q = urllib.parse.urlencode({"code": code, "state": state})
        return f"{self.redirect_uri()}?{q}"

    def exchange_code(self, code, code_verifier=None):
        if not code.startswith("mock-"):
            raise AuthError("mock: bad code")
        handle = code.removeprefix("mock-")
        return TokenSet(access_token=f"at-{handle}-{secrets.token_hex(4)}", refresh_token=f"rt-{handle}",
                        expires_at=TokenSet.expiry(3600), scopes=["mock.upload"], extra={"handle": handle})

    def refresh(self, account):
        if account.tokens.refresh_token == "revoked":
            raise AuthError("mock: refresh token revoked")
        return TokenSet(access_token=f"at-{secrets.token_hex(4)}", refresh_token=account.tokens.refresh_token,
                        expires_at=TokenSet.expiry(3600), scopes=account.tokens.scopes, extra=account.tokens.extra)

    def profile(self, account):
        handle = account.tokens.extra.get("handle", "user")
        return {"remote_id": f"mock-{handle}", "display_name": f"Mock {handle}", "username": handle,
                "avatar_url": None}

    def upload(self, account: Account, video_path: Path, post: PostSpec, ctx) -> PublishResult:
        o = post.options
        if o.get("mock_auth_fail"):
            raise AuthError("mock: token revoked by the platform")
        if o.get("mock_fail") == "permanent":
            raise PublishError("mock: video rejected (permanent)")
        failures = ctx.state.get("mock_failures", 0)
        if failures < int(o.get("mock_fail_times", 0)):
            ctx.state["mock_failures"] = failures + 1
            ctx.save()
            raise PublishError(f"mock: transient failure {failures + 1}", retryable=True, retry_after=0.05)
        size = video_path.stat().st_size
        start = ctx.state.get("offset", 0)       # "resumable": continue where the last attempt stopped
        for i in range(int(start * 4), 4):
            if ctx.cancelled():
                raise PublishError("cancelled")
            time.sleep(self.delay / 4)
            ctx.state["offset"] = (i + 1) / 4
            ctx.save()
            ctx.progress((i + 1) / 4)
        post_id = secrets.token_hex(5)
        write_json(config.data_dir() / "mock_posts" / account.id / f"{post_id}.json", {
            "account": account.id, "file": str(video_path), "bytes": size, "post": post.to_dict(),
            "caption_full": post.caption_with_tags(), "at": time.time()})
        url = f"https://mock.local/{account.username}/{post_id}"
        return PublishResult(status="processing", remote_id=post_id, url=url,
                             state={"remote_id": post_id, "url": url, "polls": 0})

    def status(self, account, result_state):
        return PublishResult(status="published", remote_id=result_state["remote_id"], url=result_state["url"])
