"""`Publisher`: ties connectors, accounts, variants and the publish queue together.

The queue is a dispatcher thread plus a thread pool. A target starts only when its platform is
under `Connector.max_concurrency` running uploads and its account has none running (one upload per
account at a time), so 3 accounts on 3 platforms upload simultaneously while one channel never gets
two parallel uploads. Failures marked retryable back off exponentially (honouring Retry-After);
upload state (YouTube session URI, TikTok chunk index, Instagram container id) is persisted, so a
retry or a server restart resumes instead of starting over.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from publisher import config, public_url
from publisher.accounts import AccountStore, PendingAuth
from publisher.appcreds import AppCredentials
from publisher.connectors import build_connectors
from publisher.connectors.base import Connector
from publisher.jobs import PublishJobStore
from publisher.tunnel import Tunnel
from publisher.models import (TERMINAL, AuthError, NotConfigured, PostSpec, PublishError, UploadContext, now,
                              parse_time, utcnow)
from publisher.variants import build_variants, prepare_video

log = logging.getLogger("clipper.publisher")

TICK_SEC = 0.25
REFRESH_EVERY_SEC = 3600


class Publisher:
    def __init__(self, data_dir: Path | None = None, connectors: dict[str, Connector] | None = None,
                 llm_factory: Callable[[], object] | None = None, ffmpeg: str | None = None,
                 url_provider=None, workers: int | None = None):
        self.data_dir = data_dir or config.data_dir()
        self.connectors = connectors if connectors is not None else build_connectors()
        self.accounts = AccountStore(self.data_dir)
        self.app_creds = AppCredentials(self.data_dir)
        for c in self.connectors.values():
            c.app_creds = self.app_creds
        self.pending = PendingAuth(self.data_dir)
        self.jobs = PublishJobStore(self.data_dir)
        self.llm_factory = llm_factory or _default_llm
        self._ffmpeg = ffmpeg
        self.url_provider = url_provider if url_provider is not None else public_url.from_env()
        # The free tunnel (Accounts page) doubles as the public URL provider when none is configured.
        self._tunnel_urls = public_url.TunnelProvider("https://unset.invalid") if self.url_provider is None else None
        self.tunnel = Tunnel(self.data_dir, on_url=self._on_tunnel_url)
        self.retry_base_sec = float(os.environ.get("PUBLISHER_RETRY_BASE", "20"))
        self.max_attempts = int(os.environ.get("PUBLISHER_MAX_ATTEMPTS", "5"))
        self.workers = workers or int(os.environ.get("PUBLISHER_WORKERS", "8"))
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running: set[tuple[str, str]] = set()
        self._by_platform: dict[str, int] = {}
        self._by_account: dict[str, int] = {}
        self._last_refresh = 0.0

    def _on_tunnel_url(self, url: str | None) -> None:
        if self._tunnel_urls is None:
            return
        if url:
            self._tunnel_urls.base_url = url.rstrip("/")
            self.url_provider = self._tunnel_urls
        else:
            self.url_provider = None

    # -- platforms & accounts -----------------------------------------------------------------

    def connector(self, platform: str) -> Connector:
        c = self.connectors.get(platform)
        if not c:
            raise KeyError(platform)
        return c

    def platforms(self) -> list[dict]:
        out = []
        for c in self.connectors.values():
            d = c.describe()
            d["accounts"] = len(self.accounts.list(c.name))
            d["blocker"] = self.connect_blocker(c)
            d["connect_ready"] = d["blocker"] is None
            if c.limits.needs_public_url and not self.url_provider:
                d["warning"] = "Posting needs a public https address (PUBLISHER_PUBLIC_BASE_URL or PUBLISHER_TUNNEL in .env)."
            out.append(d)
        return out

    def connect_blocker(self, c: Connector) -> str | None:
        """Why users can't press "Connect" yet: one-time admin setup that is still missing."""
        if not c.configured():
            return (f"{c.label} isn't switched on: set {c.client_id_env[0]} and {c.client_secret_env[0]} "
                    "in the server's .env (docs/publishing.md).")
        if c.https_redirect and not c.redirect_uri().startswith("https://"):
            return "Needs a public https address: set PUBLISHER_PUBLIC_BASE_URL or PUBLISHER_TUNNEL in .env."
        return None

    def start_oauth(self, platform: str, login_hint: str | None = None, return_to: str | None = None) -> str:
        c = self.connector(platform)
        c.require_configured()
        blocker = self.connect_blocker(c)
        if blocker:
            raise NotConfigured(blocker)
        verifier = challenge = None
        if c.supports_pkce:
            verifier, challenge = c.make_pkce()
        state = self.pending.create(platform, verifier, {"return_to": return_to})
        return c.auth_url(state, challenge, login_hint)

    def finish_oauth(self, platform: str, code: str | None, state: str | None,
                     error: str | None = None) -> tuple[dict, str | None]:
        """Handle the callback. Returns (public account, return_to)."""
        entry = self.pending.pop(state or "", platform)
        if not entry:
            raise PublishError("This sign-in link expired or was already used. Start the connection again.")
        if error or not code:
            raise PublishError(f"The platform did not grant access: {error or 'no code returned'}")
        c = self.connector(platform)
        tokens = c.exchange_code(code, entry.get("verifier"))
        from publisher.models import Account

        temp = Account(id="new", platform=platform, remote_id=tokens.extra.get("user_id") or "", tokens=tokens)
        accts = [self.accounts.upsert(platform, tokens, p) for p in c.profiles(temp)]
        for a in accts:
            log.info("connected %s account %s", platform, a["display_name"] or a["id"])
        return {**accts[0], "connected": [a["username"] or a["display_name"] for a in accts]}, entry.get("return_to")

    def disconnect(self, account_id: str, revoke: bool = True) -> None:
        rec = self.accounts.get_public(account_id)
        if not rec:
            raise KeyError(account_id)
        c = self.connectors.get(rec["platform"])
        if revoke and c:
            try:
                c.revoke(self.accounts.account(account_id))
            except Exception:
                pass
        self.accounts.delete(account_id)

    # -- variants & jobs ------------------------------------------------------------------------

    def _accounts_for(self, account_ids: list[str]) -> list[dict]:
        accts = []
        for aid in account_ids:
            a = self.accounts.get_public(aid)
            if not a:
                raise KeyError(aid)
            if a["platform"] not in self.connectors:
                raise PublishError(f"Platform {a['platform']} is not enabled")
            accts.append(a)
        return accts

    def preview(self, source: str, account_ids: list[str], clip_ids: list[int] | None = None,
                use_llm: bool = False) -> list[dict]:
        llm = self.llm_factory() if use_llm else None
        return build_variants(source, self._accounts_for(account_ids), self.connectors, clip_ids, llm)

    def create_job(self, source: str, targets: list[dict] | None = None, account_ids: list[str] | None = None,
                   clip_ids: list[int] | None = None, use_llm: bool = False,
                   scheduled_at: str | None = None) -> dict:
        """Either pass `targets` (edited variants from preview()) or let it build variants itself."""
        if targets is None:
            targets = self.preview(source, account_ids or [], clip_ids, use_llm)
        else:
            known = {(v["clip_id"], v["account_id"]): v for v in
                     self.preview(source, sorted({t["account_id"] for t in targets}),
                                  sorted({t["clip_id"] for t in targets}))}
            merged = []
            for t in targets:
                base = known.get((t["clip_id"], t["account_id"]))
                if not base:
                    raise PublishError(f"clip {t['clip_id']} / account {t['account_id']} not found")
                post = PostSpec.from_dict({**base["post"], **(t.get("post") or {})})
                merged.append({**base, "post": post.to_dict(), "scheduled_at": t.get("scheduled_at")})
            targets = merged
        if not targets:
            raise PublishError("Nothing to publish: no clips or no accounts selected")
        if scheduled_at:
            parse_time(scheduled_at)   # validate
        job = self.jobs.create(source, targets, scheduled_at, self.max_attempts)
        self.jobs.log(job["id"], "info", f"Created with {len(targets)} targets")
        return job

    def cancel(self, job_id: str) -> dict:
        def apply(job):
            for t in job["targets"]:
                if t["status"] not in TERMINAL:
                    t.update(status="cancelled", finished_at=now())
        return self.jobs.mutate(job_id, apply)

    def retry(self, job_id: str) -> dict:
        def apply(job):
            for t in job["targets"]:
                if t["status"] in ("failed", "cancelled"):
                    t.update(status="queued", attempts=0, error=None, next_attempt_at=0.0, finished_at=None)
        return self.jobs.mutate(job_id, apply)

    # -- queue ----------------------------------------------------------------------------------

    def start(self) -> None:
        if self._pool:
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="publish")
        threading.Thread(target=self._loop, name="publish-dispatcher", daemon=True).start()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        self.tunnel.stop(forget=False)
        if self._pool:
            self._pool.shutdown(wait=wait, cancel_futures=True)
            self._pool = None

    def wait_idle(self, timeout: float = 30) -> bool:
        """Block until no target is pending (scheduled targets excluded). For tests and the demo."""
        end = time.time() + timeout
        while time.time() < end:
            pending = [t for _, t in self.jobs.active_targets() if t["status"] != "scheduled"]
            if not pending:
                return True
            time.sleep(0.05)
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("publish dispatcher error")
            self._stop.wait(TICK_SEC)

    def tick(self) -> None:
        t_now = time.time()
        if t_now - self._last_refresh > REFRESH_EVERY_SEC:
            self._last_refresh = t_now
            for aid in self.accounts.refresh_due(self.connectors):
                log.warning("account %s needs to be reconnected", aid)
        for job_id, t in self.jobs.active_targets():
            key = (job_id, t["id"])
            with self._lock:
                if key in self._running:
                    continue
            if t["status"] == "scheduled":
                when = parse_time(t["scheduled_at"])
                if when and when <= utcnow():
                    self.jobs.update_target(job_id, t["id"], status="queued")
                continue
            if t["next_attempt_at"] > t_now:
                continue
            c = self.connectors.get(t["platform"])
            if not c:
                self.jobs.update_target(job_id, t["id"], status="failed", error="platform not enabled")
                continue
            if t["status"] == "processing":
                self._submit(key, t, self._poll, slots=False)
            elif t["status"] == "queued":
                self._submit(key, t, self._upload, slots=True, limit=c.max_concurrency)

    def _submit(self, key, t, fn, slots: bool, limit: int = 1) -> None:
        with self._lock:
            if slots:
                if self._by_platform.get(t["platform"], 0) >= limit or self._by_account.get(t["account_id"], 0) >= 1:
                    return
                self._by_platform[t["platform"]] = self._by_platform.get(t["platform"], 0) + 1
                self._by_account[t["account_id"]] = self._by_account.get(t["account_id"], 0) + 1
            self._running.add(key)
        if slots:
            self.jobs.update_target(key[0], key[1], status="uploading", started_at=t["started_at"] or now(),
                                    attempts=t["attempts"] + 1, error=None)
        pool = self._pool
        if pool is None:
            self._release(key, t, slots)
            return

        def run():
            try:
                fn(key[0], key[1])
            except Exception as e:  # never lose a target to an unexpected bug
                log.exception("publish target %s crashed", key)
                self.jobs.update_target(key[0], key[1], status="failed", error=f"Unexpected error: {e}",
                                        finished_at=now())
            finally:
                self._release(key, t, slots)
        pool.submit(run)

    def _release(self, key, t, slots: bool) -> None:
        with self._lock:
            self._running.discard(key)
            if slots:
                self._by_platform[t["platform"]] -= 1
                self._by_account[t["account_id"]] -= 1

    def _cancelled(self, job_id, target_id) -> bool:
        return self.jobs.target(job_id, target_id)["status"] == "cancelled"

    def _upload(self, job_id: str, target_id: str) -> None:
        t = self.jobs.target(job_id, target_id)
        c = self.connector(t["platform"])
        name = t["account_name"] or t["account_id"]
        try:
            account = self.accounts.fresh(t["account_id"], c)
            video = prepare_video(Path(t["clip_file"]), t.get("duration_sec"), c.limits,
                                  self.data_dir / "fitted", self.ffmpeg() if _needs_fix(t, c) else None)
            state = dict(t["state"])
            last = [0.0]

            def save():
                self.jobs.update_target(job_id, target_id, state=dict(state))

            def progress(f):
                if f - last[0] >= 0.1 or f >= 1:      # throttle disk writes
                    last[0] = f
                    self.jobs.update_target(job_id, target_id, progress=round(f, 3))

            ctx = UploadContext(state=state, save=save, progress=progress,
                                public_url=self.url_provider.url_for if self.url_provider else None,
                                cancelled=lambda: self._cancelled(job_id, target_id))
            self.jobs.log(job_id, "info", f"{t['id']}: uploading clip {t['clip_id']} to {t['platform']} / {name}")
            res = c.upload(account, video, PostSpec.from_dict(t["post"]), ctx)
            if self._cancelled(job_id, target_id):
                return
            self._apply_result(job_id, target_id, res, c)
        except PublishError as e:
            self._fail(job_id, target_id, e, c)

    def _poll(self, job_id: str, target_id: str) -> None:
        t = self.jobs.target(job_id, target_id)
        c = self.connector(t["platform"])
        try:
            started = parse_time(t["processing_since"]) if t.get("processing_since") else None
            if started and (utcnow() - started).total_seconds() > c.max_processing_sec:
                raise PublishError(f"{c.label} still processing after {c.max_processing_sec / 60:.0f} min")
            account = self.accounts.fresh(t["account_id"], c)
            res = c.status(account, t["result_state"] or {})
            self._apply_result(job_id, target_id, res, c)
        except PublishError as e:
            if e.retryable and not isinstance(e, AuthError):
                self.jobs.update_target(job_id, target_id, next_attempt_at=time.time() + (e.retry_after or 30),
                                        message=f"status check failed, will retry: {e}")
            else:
                self._fail(job_id, target_id, e, c, final=True)

    def _apply_result(self, job_id, target_id, res, c: Connector) -> None:
        fields = {"remote_id": res.remote_id, "message": res.message, "progress": 1.0}
        if res.url:
            fields["url"] = res.url
        if res.status == "published":
            self.jobs.update_target(job_id, target_id, status="published", finished_at=now(), result_state=None,
                                    **fields)
            t = self.jobs.target(job_id, target_id)
            self.jobs.log(job_id, "info", f"{target_id}: published {t.get('url') or t.get('remote_id')}")
        else:
            t = self.jobs.target(job_id, target_id)
            self.jobs.update_target(job_id, target_id, status="processing", result_state=res.state,
                                    processing_since=t.get("processing_since") or now(),
                                    next_attempt_at=time.time() + c.poll_interval_sec, **fields)

    def _fail(self, job_id, target_id, e: PublishError, c: Connector, final: bool = False) -> None:
        t = self.jobs.target(job_id, target_id)
        if str(e) == "cancelled" or t["status"] == "cancelled":
            return
        if isinstance(e, AuthError):
            self.accounts.mark_reconnect(t["account_id"], str(e))
            msg = f"Reconnect {t['account_name'] or t['account_id']}: {e}"
            self.jobs.update_target(job_id, target_id, status="failed", error=msg, finished_at=now())
            self.jobs.log(job_id, "error", f"{target_id}: {msg}")
            return
        if e.retryable and not final and t["attempts"] < t["max_attempts"]:
            wait = e.retry_after if e.retry_after is not None else \
                min(900.0, self.retry_base_sec * 2 ** (t["attempts"] - 1)) * random.uniform(0.8, 1.2)
            self.jobs.update_target(job_id, target_id, status="queued", next_attempt_at=time.time() + wait,
                                    error=str(e), message=f"retrying in {wait:.0f}s")
            self.jobs.log(job_id, "warning", f"{target_id}: {e} (attempt {t['attempts']}, retry in {wait:.0f}s)")
            return
        self.jobs.update_target(job_id, target_id, status="failed", error=str(e), finished_at=now())
        self.jobs.log(job_id, "error", f"{target_id}: {e}")

    def ffmpeg(self) -> str | None:
        if self._ffmpeg is None:
            try:
                from clipper.util import find_ffmpeg

                self._ffmpeg = find_ffmpeg(None, config.REPO_ROOT / "work")
            except Exception:
                self._ffmpeg = ""
        return self._ffmpeg or None


def _needs_fix(t: dict, c: Connector) -> bool:
    d = t.get("duration_sec") or 0
    return d > c.limits.max_duration_sec or bool(c.limits.max_file_mb)


def _default_llm():
    from clipper.config import load_config
    from clipper.llm import LLM

    return LLM(load_config(config.REPO_ROOT / "config.yaml")["llm"])


__all__ = ["Publisher", "NotConfigured"]
