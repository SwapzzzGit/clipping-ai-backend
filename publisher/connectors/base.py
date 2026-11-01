"""The connector interface every platform implements.

A connector is stateless apart from its HTTP client and app credentials: tokens come in on the
`Account`, and anything an upload must remember between attempts goes in `UploadContext.state`.
Adding a platform (Facebook Reels, LinkedIn, X, ...) = subclass `Connector`, declare `limits`,
and register it in `publisher/connectors/__init__.py`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from publisher import config
from publisher.models import Account, AuthError, Limits, NotConfigured, PostSpec, PublishError, PublishResult, TokenSet

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class Connector(ABC):
    name: str = ""                  # url-safe id: "youtube", "tiktok", ...
    label: str = ""
    limits: Limits
    supports_pkce: bool = False
    client_id_env: tuple[str, ...] = ()
    client_secret_env: tuple[str, ...] = ()
    # Refresh this many seconds before the access token expires.
    refresh_margin_sec: float = 300
    # Upload / processing concurrency the queue allows per platform.
    max_concurrency: int = 2
    # How often the queue polls status() while a post is "processing", and when it gives up.
    poll_interval_sec: float = 10
    max_processing_sec: float = 3600

    def __init__(self, http: httpx.Client | None = None, client_id: str | None = None,
                 client_secret: str | None = None):
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=False)
        self._client_id = client_id
        self._client_secret = client_secret
        # Set by Publisher: app id/secret the user typed into the UI (publisher/appcreds.py).
        self.app_creds = None

    # -- app credentials --------------------------------------------------------------------

    @property
    def client_id(self) -> str:
        return self._client_id or config.env(*self.client_id_env) or self._stored()[0]

    @property
    def client_secret(self) -> str:
        return self._client_secret or config.env(*self.client_secret_env) or self._stored()[1]

    def _stored(self) -> tuple[str, str]:
        return self.app_creds.get(self.name) if self.app_creds else ("", "")

    def credentials_source(self) -> str:
        """"env" | "ui" | "" (not configured), for the setup screen."""
        if self._client_id or config.env(*self.client_id_env):
            return "env"
        return "ui" if any(self._stored()) else ""

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def require_configured(self) -> None:
        if not self.configured():
            raise NotConfigured(f"{self.label} is not set up: enter the app ID and secret on the Accounts page, "
                                f"or set {self.client_id_env[0]} and {self.client_secret_env[0]} (see docs/publishing.md).")

    # Non-secret app settings beyond id/secret, shown in Settings (key -> label).
    extra_fields: dict[str, str] = {}

    # Platforms whose app registration only accepts https redirect URIs.
    https_redirect: bool = False

    def redirect_uri(self) -> str:
        return config.redirect_uri(self.name, self.https_redirect)

    def describe(self) -> dict:
        return {"id": self.name, "label": self.label, "configured": self.configured(),
                "credentials": self.credentials_source(),
                "redirect_uri": self.redirect_uri(), "env": [self.client_id_env[0], self.client_secret_env[0]]
                if self.client_id_env else [], "limits": self.limits.to_dict()}

    # -- OAuth ------------------------------------------------------------------------------

    @abstractmethod
    def auth_url(self, state: str, code_challenge: str | None = None, login_hint: str | None = None) -> str:
        """URL of the platform's consent screen."""

    @abstractmethod
    def exchange_code(self, code: str, code_verifier: str | None = None) -> TokenSet:
        """Trade the callback's `code` for tokens."""

    @abstractmethod
    def refresh(self, account: Account) -> TokenSet:
        """Return fresh tokens. Raise AuthError when the grant is gone (user must reconnect)."""

    @abstractmethod
    def profile(self, account: Account) -> dict:
        """{"remote_id", "display_name", "username", "avatar_url"} of the connected account."""

    def profiles(self, account: Account) -> list[dict]:
        """Every platform account one login granted (default: just the one)."""
        return [self.profile(account)]

    def make_pkce(self) -> tuple[str, str]:
        """(verifier, challenge) per RFC 7636 S256. Platforms with quirks override this."""
        verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(verifier.encode()).digest()
        return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def should_refresh(self, tokens: TokenSet, now_ts: float) -> bool:
        from publisher.models import parse_time

        exp = parse_time(tokens.expires_at)
        return bool(exp and tokens.refresh_token is not None and exp.timestamp() - now_ts < self.refresh_margin_sec)

    def revoke(self, account: Account) -> None:
        """Best-effort token revocation on disconnect."""

    # -- publishing -------------------------------------------------------------------------

    @abstractmethod
    def upload(self, account: Account, video_path: Path, post: PostSpec, ctx) -> PublishResult:
        """Upload and publish (or start publishing). Return status "processing" when the platform
        still has work to do; the queue then calls status() until it reports "published"."""

    def status(self, account: Account, result_state: dict) -> PublishResult:
        """Poll a post that upload() left in "processing"."""
        return PublishResult(status="published", **{k: result_state.get(k) for k in ("remote_id", "url")})

    # -- HTTP helpers -----------------------------------------------------------------------

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        """Map HTTP failures onto retryable / auth / permanent errors."""
        if resp.status_code < 400:
            return resp
        detail = resp.text[:500]
        if resp.status_code == 401:
            raise AuthError(f"{self.label}: {what} rejected the token (HTTP 401): {detail}")
        if resp.status_code in RETRY_STATUS:
            ra = resp.headers.get("retry-after")
            raise PublishError(f"{self.label}: {what} HTTP {resp.status_code}: {detail}", retryable=True,
                               retry_after=float(ra) if ra and ra.isdigit() else None)
        raise PublishError(f"{self.label}: {what} HTTP {resp.status_code}: {detail}")

    def _request(self, method: str, url: str, what: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, url, **kw)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise PublishError(f"{self.label}: {what} network error: {e}", retryable=True)
        return self._check(resp, what)
