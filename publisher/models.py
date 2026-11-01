"""Plain data types shared by connectors, stores, the queue and the API."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

TARGET_STATES = ["scheduled", "queued", "uploading", "processing", "published", "failed", "cancelled"]
TERMINAL = {"published", "failed", "cancelled"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# -- errors --------------------------------------------------------------------------------

class PublishError(RuntimeError):
    """An upload/publish failure. `retryable` decides whether the queue backs off and tries again."""

    def __init__(self, message: str, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class AuthError(PublishError):
    """The platform rejected the account's tokens: the user must reconnect it."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class NotConfigured(PublishError):
    """The developer app's client id/secret env vars are missing."""


# -- platform rules --------------------------------------------------------------------------

@dataclass
class Limits:
    max_duration_sec: float
    min_duration_sec: float = 1.0
    aspect_ratios: list[str] = field(default_factory=lambda: ["9:16"])
    max_file_mb: float | None = None
    title_max: int | None = None             # None = platform has no separate title
    caption_max: int = 2200
    hashtag_max: int | None = None
    hashtags_in: str = "caption"             # "caption" | "description"
    required_hashtags: list[str] = field(default_factory=list)
    privacy_options: list[str] = field(default_factory=lambda: ["public", "private"])
    private_only_unaudited: bool = False     # posts are forced private until the app passes review
    supports_resumable: bool = False
    needs_public_url: bool = False           # platform pulls the video from a URL
    daily_post_limit: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PostSpec:
    """One platform-tailored post of one clip. Editable by the user before publishing."""

    title: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    privacy: str = "public"
    options: dict[str, Any] = field(default_factory=dict)   # platform-specific knobs

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PostSpec":
        known = {k: d[k] for k in ("title", "caption", "hashtags", "privacy", "options") if k in d}
        return cls(**known)

    def caption_with_tags(self) -> str:
        """Caption text with hashtags appended (skipping any already in the text)."""
        text = self.caption.strip()
        low = text.lower()
        tags = [t for t in self.hashtags if t.lower() not in low]
        return (text + ("\n\n" if text and tags else "") + " ".join(tags)).strip()


@dataclass
class PublishResult:
    status: str                   # "processing" | "published"
    remote_id: str | None = None
    url: str | None = None
    message: str = ""
    state: dict = field(default_factory=dict)   # what status() needs later (container ids, publish ids)


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: str | None = None          # ISO time
    refresh_expires_at: str | None = None
    scopes: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # platform ids that come with the token (open_id, user_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TokenSet":
        return cls(**d)

    @staticmethod
    def expiry(seconds: float | None) -> str | None:
        if not seconds:
            return None
        return (utcnow() + dt.timedelta(seconds=float(seconds))).isoformat(timespec="seconds")


@dataclass
class Account:
    """A connected account with its (decrypted) tokens, as handed to a connector."""

    id: str
    platform: str
    remote_id: str
    tokens: TokenSet
    display_name: str = ""
    username: str = ""
    options: dict = field(default_factory=dict)


@dataclass
class UploadContext:
    """Per-attempt helpers passed to Connector.upload."""

    state: dict                                     # persisted across attempts and restarts (resume)
    save: Callable[[], None] = lambda: None         # persist `state` now
    progress: Callable[[float], None] = lambda f: None
    public_url: Callable[[str], str] | None = None  # local path -> temporary public https URL
    cancelled: Callable[[], bool] = lambda: False
