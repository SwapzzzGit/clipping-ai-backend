"""Password protection for a deployed Clipper Studio.

Set APP_PASSWORD on the server to turn it on (unset = local dev, no login). Signing in sets an
HttpOnly session cookie signed with a key derived from the password, so it survives restarts and
every session ends when the password changes. Only the platform OAuth callbacks, the temporary
publish media links and the health check stay reachable without signing in.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

COOKIE = "clipper_session"
SESSION_DAYS = 30
PUBLIC = re.compile(r"^/api/(?:auth/(?:status|login|logout)|health"
                    r"|publish/(?:oauth/[a-z0-9_-]+/callback|media/[A-Za-z0-9_-]+/[^/]+|ping))$")


def _password() -> str:
    return os.environ.get("APP_PASSWORD", "")


def _key() -> bytes:
    return hashlib.sha256(b"clipper-session:" + _password().encode()).digest()


def _sign(expires: int) -> str:
    sig = hmac.new(_key(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(int(exp)).partition(".")[2])


def _bearer_ok(request: Request) -> bool:
    """Scripts (backend/push_clips.py) send `Authorization: Bearer <APP_PASSWORD>` instead of a cookie."""
    header = request.headers.get("authorization", "")
    return header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), _password().encode())


def authenticated(request: Request) -> bool:
    return not _password() or _valid(request.cookies.get(COOKIE)) or _bearer_ok(request)


class LoginIn(BaseModel):
    password: str


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def status(request: Request):
    return {"required": bool(_password()), "authenticated": authenticated(request)}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    if not _password() or not hmac.compare_digest(body.password.encode(), _password().encode()):
        await asyncio.sleep(1.0)   # slow down guessing
        return JSONResponse({"detail": "Wrong password"}, status_code=401)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, _sign(int(time.time()) + SESSION_DAYS * 86400), max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=secure, samesite="lax", path="/")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


def install(app) -> None:
    """Reject API and media requests without a session (when APP_PASSWORD is set)."""

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if (_password() and request.method != "OPTIONS" and (path.startswith("/api/") or path.startswith("/media/"))
                and not PUBLIC.match(path) and not authenticated(request)):
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        return await call_next(request)
