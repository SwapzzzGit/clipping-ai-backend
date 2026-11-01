"""Multi-account store: any number of accounts per platform, tokens Fernet-encrypted at rest.

One JSON file per account under <data_dir>/accounts/. Public views never include tokens.
Account `status`: "connected" | "reconnect_needed" (refresh failed or the platform revoked access).
Pending OAuth handshakes (state -> platform + PKCE verifier) live in <data_dir>/oauth_pending.json
so a callback still works after a server restart; each state is single-use and expires in 15 min.
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from clipper.util import read_json, write_json
from publisher import crypto
from publisher.connectors.base import Connector
from publisher.models import Account, AuthError, PublishError, TokenSet, now

STATE_TTL_SEC = 15 * 60


class AccountStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.dir = data_dir / "accounts"
        crypto.protect_dir(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._accounts: dict[str, dict] = {}
        for f in self.dir.glob("*.json"):
            rec = read_json(f)
            self._accounts[rec["id"]] = rec

    # -- records ----------------------------------------------------------------------------

    def list(self, platform: str | None = None) -> list[dict]:
        with self._lock:
            recs = [r for r in self._accounts.values() if not platform or r["platform"] == platform]
        return sorted((public(r) for r in recs), key=lambda r: (r["platform"], r["created_at"]))

    def get_public(self, account_id: str) -> dict | None:
        with self._lock:
            r = self._accounts.get(account_id)
            return public(r) if r else None

    def account(self, account_id: str) -> Account:
        """Decrypted account for a connector call."""
        with self._lock:
            r = self._accounts.get(account_id)
            if not r:
                raise KeyError(account_id)
            tokens = TokenSet.from_dict(crypto.decrypt(self.data_dir, r["tokens"]))
            return Account(id=r["id"], platform=r["platform"], remote_id=r["remote_id"], tokens=tokens,
                           display_name=r["display_name"], username=r["username"], options=dict(r["options"]))

    def upsert(self, platform: str, tokens: TokenSet, profile: dict) -> dict:
        """Add an account, or update it when the same platform account connects again."""
        with self._lock:
            existing = next((r for r in self._accounts.values()
                             if r["platform"] == platform and r["remote_id"] == profile["remote_id"]), None)
            rec = existing or {"id": f"{platform}-{secrets.token_hex(4)}", "platform": platform,
                               "created_at": now(), "options": {}}
            rec.update(remote_id=profile["remote_id"], display_name=profile.get("display_name") or "",
                       username=profile.get("username") or "", avatar_url=profile.get("avatar_url"),
                       status="connected", error=None, connected_at=now())
            self._set_tokens(rec, tokens)
            self._accounts[rec["id"]] = rec
            self._save(rec)
            return public(rec)

    def set_options(self, account_id: str, options: dict) -> dict:
        with self._lock:
            rec = self._accounts[account_id]
            rec["options"] = {**rec["options"], **options}
            self._save(rec)
            return public(rec)

    def mark_reconnect(self, account_id: str, error: str) -> None:
        with self._lock:
            rec = self._accounts.get(account_id)
            if rec:
                rec.update(status="reconnect_needed", error=error)
                self._save(rec)

    def delete(self, account_id: str) -> None:
        with self._lock:
            self._accounts.pop(account_id, None)
            (self.dir / f"{account_id}.json").unlink(missing_ok=True)

    # -- tokens -----------------------------------------------------------------------------

    def fresh(self, account_id: str, connector: Connector, force: bool = False) -> Account:
        """The account with a valid access token, refreshing first if it is about to expire.
        Raises AuthError (and flags the account) when the user has to reconnect."""
        rec = self._accounts.get(account_id)
        if rec is None:
            raise KeyError(account_id)
        if rec["status"] == "reconnect_needed":
            raise AuthError(f"{rec['display_name'] or account_id} needs to be reconnected: {rec.get('error')}")
        with self._lock:
            lock = self._refresh_locks.setdefault(account_id, threading.Lock())
        with lock:   # one refresh per account at a time; concurrent uploads wait and reuse it
            acct = self.account(account_id)
            if not force and not connector.should_refresh(acct.tokens, time.time()):
                return acct
            try:
                tokens = connector.refresh(acct)
            except AuthError as e:
                self.mark_reconnect(account_id, str(e))
                raise
            with self._lock:
                rec = self._accounts[account_id]
                self._set_tokens(rec, tokens)
                rec.update(status="connected", error=None, refreshed_at=now())
                self._save(rec)
            acct.tokens = tokens
            return acct

    def refresh_due(self, connectors: dict[str, Connector]) -> list[str]:
        """Refresh every connected account whose token is close to expiry (keeps idle 60-day
        Instagram tokens alive). Returns ids that now need reconnecting."""
        lost = []
        for rec in list(self._accounts.values()):
            c = connectors.get(rec["platform"])
            if not c or rec["status"] != "connected":
                continue
            try:
                self.fresh(rec["id"], c)
            except AuthError:
                lost.append(rec["id"])
            except PublishError:
                pass     # network trouble: try again next round
        return lost

    def _set_tokens(self, rec: dict, tokens: TokenSet) -> None:
        rec["tokens"] = crypto.encrypt(self.data_dir, tokens.to_dict())
        rec["token_expires_at"] = tokens.expires_at
        rec["refresh_expires_at"] = tokens.refresh_expires_at
        rec["scopes"] = tokens.scopes
        rec["updated_at"] = now()

    def _save(self, rec: dict) -> None:
        write_json(self.dir / f"{rec['id']}.json", rec)


def public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "tokens"}


class PendingAuth:
    """OAuth `state` values in flight, with the PKCE verifier that goes with each."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "oauth_pending.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        data = read_json(self.path) if self.path.is_file() else {}
        cutoff = time.time() - STATE_TTL_SEC
        return {k: v for k, v in data.items() if v["created"] > cutoff}

    def create(self, platform: str, verifier: str | None, extra: dict | None = None) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            data = self._load()
            data[state] = {"platform": platform, "verifier": verifier, "created": time.time(), **(extra or {})}
            write_json(self.path, data)
        return state

    def pop(self, state: str, platform: str) -> dict | None:
        with self._lock:
            data = self._load()
            entry = data.pop(state, None)
            write_json(self.path, data)
        if not entry or entry["platform"] != platform:
            return None
        return entry
