"""Developer-app credentials (client id + secret) entered in the UI instead of env vars.

Stored in <data_dir>/app_credentials.json; the secret is Fernet-encrypted like account tokens.
Env vars, when set, take precedence (see Connector.client_id).
"""

from __future__ import annotations

import threading
from pathlib import Path

from clipper.util import read_json, write_json
from publisher import crypto
from publisher.models import now


class AppCredentials:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "app_credentials.json"
        self._lock = threading.Lock()
        self._data: dict[str, dict] = read_json(self.path) if self.path.is_file() else {}

    def get(self, platform: str) -> tuple[str, str]:
        with self._lock:
            rec = self._data.get(platform)
        if not rec:
            return "", ""
        try:
            secret = crypto.decrypt(self.data_dir, rec["secret"])["v"] if rec.get("secret") else ""
        except crypto.SecretError:
            secret = ""
        return rec["client_id"], secret

    def get_extra(self, platform: str) -> dict:
        with self._lock:
            return dict((self._data.get(platform) or {}).get("extra") or {})

    def set(self, platform: str, client_id: str, client_secret: str | None, extra: dict | None = None) -> None:
        """Save; `client_secret=None` keeps the stored secret (or leaves it unset, so an admin can
        enter the public IDs first and paste the secret later). `extra` holds non-secret settings
        (e.g. Meta's configuration ID); keys given as "" are removed."""
        with self._lock:
            old = self._data.get(platform, {})
            secret = old.get("secret") if client_secret is None else crypto.encrypt(self.data_dir, {"v": client_secret})
            merged = {**(old.get("extra") or {}), **{k: v.strip() for k, v in (extra or {}).items()}}
            self._data[platform] = {"client_id": client_id.strip(), "secret": secret, "updated_at": now(),
                                    "extra": {k: v for k, v in merged.items() if v}}
            write_json(self.path, self._data)

    def delete(self, platform: str) -> None:
        with self._lock:
            self._data.pop(platform, None)
            write_json(self.path, self._data)

    def public(self, platform: str) -> dict:
        cid, secret = self.get(platform)
        return {"client_id": cid, "has_secret": bool(secret), "secret_hint": f"…{secret[-4:]}" if len(secret) >= 8 else "",
                "extra": self.get_extra(platform)}
