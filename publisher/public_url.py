"""Temporary public URLs for platforms that pull the video themselves (Instagram).

Default provider, `TunnelProvider`: this server serves the file at
`/api/publish/media/<token>/<name>` for a limited time, and PUBLISHER_PUBLIC_BASE_URL is an https
origin that reaches this server (e.g. `cloudflared tunnel --url http://localhost:8000` or ngrok).
Only registered files are served, by unguessable token, until they expire.

Plug in another provider (S3/R2 presigned URLs, a CDN, ...) by setting
PUBLISHER_PUBLIC_URL_PROVIDER="package.module:ClassName"; the class is built with no arguments and
must implement `url_for(path: str) -> str`.
"""

from __future__ import annotations

import importlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Protocol

DEFAULT_TTL = 3 * 3600   # Instagram may take a while to fetch; containers themselves expire in 24 h


class PublicUrlProvider(Protocol):
    def url_for(self, path: str) -> str: ...


class TunnelProvider:
    def __init__(self, base_url: str, ttl: float = DEFAULT_TTL):
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl
        self._lock = threading.Lock()
        self._files: dict[str, tuple[Path, float]] = {}

    def url_for(self, path: str) -> str:
        token = secrets.token_urlsafe(24)
        p = Path(path).resolve()
        with self._lock:
            self._purge()
            self._files[token] = (p, time.time() + self.ttl)
        return f"{self.base_url}/api/publish/media/{token}/{p.name}"

    def resolve(self, token: str) -> Path | None:
        with self._lock:
            self._purge()
            entry = self._files.get(token)
        return entry[0] if entry and entry[0].is_file() else None

    def _purge(self):
        t = time.time()
        for k in [k for k, (_, exp) in self._files.items() if exp < t]:
            del self._files[k]


def from_env() -> PublicUrlProvider | None:
    spec = os.environ.get("PUBLISHER_PUBLIC_URL_PROVIDER")
    if spec:
        mod, _, cls = spec.partition(":")
        return getattr(importlib.import_module(mod), cls)()
    base = os.environ.get("PUBLISHER_PUBLIC_BASE_URL")
    return TunnelProvider(base) if base else None
