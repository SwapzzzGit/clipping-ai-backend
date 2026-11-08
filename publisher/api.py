"""FastAPI router for the publishing layer. Mount it in the backend:

    from publisher.api import build_router
    from publisher.service import Publisher
    publisher = Publisher()
    app.include_router(build_router(publisher), prefix="/api/publish")
    # lifespan: publisher.start() ... publisher.stop()

Endpoints (all JSON unless noted):
  GET    /platforms                          platforms, whether configured, limits, redirect URI
  GET    /accounts[?platform=]               connected accounts (never tokens)
  PATCH  /accounts/{id}                      {"options": {...}} default hashtags/privacy/post_mode
  DELETE /accounts/{id}                      disconnect (and revoke where the platform allows)
  GET    /oauth/{platform}/start             -> {"auth_url"}; ?redirect=1 answers 302 instead
  GET    /oauth/{platform}/callback          the platform redirects here (HTML page)
  POST   /variants                           preview per-platform posts for clips x accounts
  POST   /jobs                               create a publish job (optionally scheduled)
  GET    /jobs, GET /jobs/{id}               job + per-target status, post URLs
  POST   /jobs/{id}/cancel | /jobs/{id}/retry
  DELETE /jobs/{id}
  GET    /media/{token}/{name}               temporary public file for Instagram (tunnel provider)
"""

from __future__ import annotations

import html
import json

import os
import re
import threading

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from publisher import config
from publisher.models import NotConfigured, PublishError
from publisher.public_url import TunnelProvider
from publisher.service import Publisher


class VariantsIn(BaseModel):
    source: str = Field(..., description="backend job id, CLI video id, or a folder with manifest.json")
    account_ids: list[str]
    clip_ids: list[int] | None = None
    use_llm: bool = False


class AccountPatch(BaseModel):
    options: dict


class CredentialsIn(BaseModel):
    client_id: str = Field(..., min_length=3, max_length=200)
    client_secret: str | None = Field(None, max_length=500)   # None = keep the stored secret
    extra: dict[str, str] | None = None                       # e.g. {"config_id": "..."} (not secret)


class TunnelIn(BaseModel):
    provider: str | None = None


def build_router(pub: Publisher) -> APIRouter:
    r = APIRouter(tags=["publish"])

    def errors(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except NotConfigured as e:
            raise HTTPException(409, str(e))
        except KeyError as e:
            raise HTTPException(404, f"Not found: {e.args[0] if e.args else ''}")
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except (PublishError, ValueError) as e:
            raise HTTPException(400, str(e))

    @r.get("/platforms")
    def platforms():
        return pub.platforms()

    @r.get("/accounts")
    def accounts(platform: str | None = None):
        return pub.accounts.list(platform)

    @r.patch("/accounts/{account_id}")
    def patch_account(account_id: str, body: AccountPatch):
        return errors(pub.accounts.set_options, account_id, body.options)

    @r.delete("/accounts/{account_id}", status_code=204)
    def delete_account(account_id: str):
        errors(pub.disconnect, account_id)

    @r.get("/oauth/{platform}/start")
    def oauth_start(platform: str, login_hint: str | None = None, return_to: str | None = None,
                    redirect: bool = False):
        url = errors(pub.start_oauth, platform, login_hint, _safe_return(return_to))
        return RedirectResponse(url, 302) if redirect else {"auth_url": url}

    @r.get("/oauth/{platform}/callback", response_class=HTMLResponse)
    def oauth_callback(platform: str, code: str | None = None, state: str | None = None,
                       error: str | None = None, error_description: str | None = None):
        try:
            acct, return_to = pub.finish_oauth(platform, code, state,
                                               error and f"{error}: {error_description or ''}")
        except (PublishError, KeyError) as e:
            return HTMLResponse(_page(False, f"Could not connect {platform}: {e}", None, None), status_code=400)
        names = ", ".join(f"@{n}" for n in acct.get("connected") or []) or acct["display_name"] or acct["id"]
        return HTMLResponse(_page(True, f"Connected {names}.", acct,
                                  return_to or config.ui_url()))

    @r.post("/variants")
    def variants(body: VariantsIn):
        return errors(pub.preview, body.source, body.account_ids, body.clip_ids, body.use_llm)

    @r.get("/jobs")
    def list_jobs(source: str | None = None):
        jobs = pub.jobs.list()
        return [j for j in jobs if not source or j["source"] == source]

    @r.get("/jobs/{job_id}")
    def get_job(job_id: str):
        job = pub.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Publish job not found")
        return job

    @r.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return errors(pub.cancel, job_id)

    @r.post("/jobs/{job_id}/retry")
    def retry(job_id: str):
        return errors(pub.retry, job_id)

    @r.delete("/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str):
        pub.cancel(job_id) if pub.jobs.get(job_id) else None
        pub.jobs.delete(job_id)

    # -- guided setup (Instagram first): app credentials + free public link ---------------------

    @r.get("/setup/{platform}")
    def setup(platform: str):
        c = errors(pub.connector, platform)
        accts = pub.accounts.list(platform)
        return {"platform": c.describe(), "credentials": pub.app_creds.public(platform),
                "credentials_source": c.credentials_source(), "redirect_uri": c.redirect_uri(),
                "https_redirect": c.https_redirect, "needs_public_url": c.limits.needs_public_url,
                "extra_fields": c.extra_fields,
                "public_url_ready": bool(pub.url_provider), "tunnel": pub.tunnel.status(), "accounts": accts}

    @r.put("/setup/{platform}/credentials")
    def save_credentials(platform: str, body: CredentialsIn):
        errors(pub.connector, platform)
        allowed = pub.connector(platform).extra_fields
        extra = {k: v for k, v in (body.extra or {}).items() if k in allowed}
        errors(pub.app_creds.set, platform, body.client_id, body.client_secret or None, extra)
        return setup(platform)

    @r.delete("/setup/{platform}/credentials")
    def delete_credentials(platform: str):
        pub.app_creds.delete(platform)
        return setup(platform)

    @r.get("/tunnel")
    def tunnel_status():
        return pub.tunnel.status()

    @r.post("/tunnel/start")
    def tunnel_start(request: Request, body: TunnelIn | None = None):
        try:
            pub.tunnel.start(_server_port(request), (body.provider if body else None))
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e))
        pub.tunnel.wait_ready(25)
        return pub.tunnel.status()

    @r.post("/tunnel/stop")
    def tunnel_stop():
        return pub.tunnel.stop()

    @r.post("/tunnel/check")
    def tunnel_check():
        """Fetch our own /ping through the public link, to prove the outside world can reach us."""
        base = config.public_base()
        if not base:
            return {"ok": False, "error": "The public link is not running"}
        try:
            resp = httpx.get(f"{base}/api/publish/ping", timeout=15, follow_redirects=True)
            ok = resp.status_code == 200 and resp.json().get("ok") is True
            return {"ok": ok, "url": base, "error": None if ok else f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "url": base, "error": str(e)}

    @r.get("/ping")
    def ping():
        return {"ok": True, "service": "clipper-publisher"}

    @r.get("/media/{token}/{name}", include_in_schema=False)
    def media(token: str, name: str):
        prov = pub._tunnel_urls or pub.url_provider
        path = prov.resolve(token) if isinstance(prov, TunnelProvider) else None
        if not path or path.name != name:
            raise HTTPException(404, "expired or unknown")
        return FileResponse(path, media_type="video/mp4")

    return r


LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "cf-connecting-ip", "x-real-ip",
                   "ngrok-trace-id")
PUBLIC_PATHS = re.compile(r"^/api/publish/(?:oauth/[a-z0-9_-]+/callback|media/[A-Za-z0-9_-]+/[^/]+|ping)$")


def _server_port(request: Request) -> int:
    server = request.scope.get("server")
    return int(server[1]) if server else 8000


def is_external(request: Request) -> bool:
    """True for requests that arrive through a tunnel or from another machine."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if host.startswith("[") and "]" in (request.headers.get("host") or ""):
        host = request.headers["host"].split("]")[0] + "]"
    if host not in LOCAL_HOSTS:
        return True
    client = request.client.host if request.client else "127.0.0.1"
    if client not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return True
    return any(h in request.headers for h in FORWARD_HEADERS)


def install_tunnel_guard(app, pub: Publisher) -> None:
    """Only the OAuth callbacks and temporary clip links may be reached through the public link;
    everything else (job API, accounts, settings, the UI) answers 404 to outside requests.
    Also learns this server's local origin and restarts a remembered tunnel on first use."""
    started = threading.Event()

    @app.middleware("http")
    async def tunnel_guard(request: Request, call_next):
        if pub.tunnel.state != "off" and is_external(request):
            if request.method not in ("GET", "HEAD") or not PUBLIC_PATHS.match(request.url.path):
                from fastapi.responses import PlainTextResponse

                return PlainTextResponse("Not found", status_code=404)
        elif not started.is_set():
            started.set()
            port = _server_port(request)
            config.set_local_base(f"http://localhost:{port}")
            provider = os.environ.get("PUBLISHER_TUNNEL", "").strip()
            if provider and not config.public_base() and pub.tunnel.state == "off":
                threading.Thread(target=pub.tunnel.start, args=(port, provider, False), daemon=True).start()
        return await call_next(request)


def _safe_return(url: str | None) -> str | None:
    """Only bounce back to our own UI after OAuth (no open redirects)."""
    if not url:
        return None
    allowed = [o for o in (config.ui_url(), "http://localhost:", "http://127.0.0.1:") if o]
    return url if any(url.startswith(a) for a in allowed) else None


def _page(ok: bool, message: str, account: dict | None, return_to: str | None) -> str:
    slim = {k: account.get(k) for k in ("id", "platform", "display_name", "username")} if account else None
    payload = json.dumps({"type": "clipper-publish-oauth", "ok": ok, "account": slim})
    redirect = f'setTimeout(function(){{location.href={json.dumps(return_to)};}}, 1200);' if return_to else ""
    return f"""<!doctype html><meta charset="utf-8"><title>{'Connected' if ok else 'Connection failed'}</title>
<body style="font-family:system-ui;background:#111;color:#eee;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h2>{'✓' if ok else '✗'} {html.escape(message)}</h2>
<p>You can close this window.</p></div>
<script>try{{window.opener&&window.opener.postMessage({payload},"*");}}catch(e){{}}
{redirect}if(window.opener){{setTimeout(function(){{window.close();}},1500);}}</script></body>"""
