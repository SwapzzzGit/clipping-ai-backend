"""Paths and env-driven settings for the publisher. Everything is overridable by env vars."""

from __future__ import annotations

import os
from pathlib import Path

from publisher import REPO_ROOT


def load_dotenv(path: Path | None = None) -> None:
    """Server-side settings (Meta app ID/secret, public URL, ...) live in <repo>/.env, which is
    gitignored. Real environment variables win over the file."""
    path = path or Path(os.environ.get("CLIPPER_ENV_FILE", REPO_ROOT / ".env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


load_dotenv()


def data_dir() -> Path:
    """<CLIPPER_DATA>/publish by default, next to the backend's own job store."""
    explicit = os.environ.get("PUBLISHER_DATA")
    if explicit:
        return Path(explicit).resolve()
    base = Path(os.environ.get("CLIPPER_DATA", REPO_ROOT / "backend" / "data"))
    return (base / "publish").resolve()


# Filled in at runtime: the origin this server answers on locally (learned from the first request)
# and the https origin of the public tunnel when one is running (publisher/tunnel.py).
_runtime: dict[str, str | None] = {"local": None, "public": None}


def set_local_base(url: str | None) -> None:
    _runtime["local"] = url.rstrip("/") if url else None


def set_public_base(url: str | None) -> None:
    _runtime["public"] = url.rstrip("/") if url else None


def public_base() -> str | None:
    return os.environ.get("PUBLISHER_PUBLIC_BASE_URL", "").rstrip("/") or _runtime["public"]


def redirect_base(https: bool = False) -> str:
    """Origin the platforms redirect back to after consent (must match the app registration).

    PUBLISHER_REDIRECT_BASE wins. Otherwise platforms that insist on https (Meta, TikTok web apps)
    use the running public tunnel, and Google gets this server's own http://localhost origin.
    """
    explicit = os.environ.get("PUBLISHER_REDIRECT_BASE")
    if explicit:
        return explicit.rstrip("/")
    if https and public_base():
        return public_base()
    return _runtime["local"] or "http://localhost:8000"


def redirect_uri(platform: str, https: bool = False) -> str:
    return f"{redirect_base(https)}/api/publish/oauth/{platform}/callback"


def ui_url() -> str | None:
    """Where the OAuth callback page sends the browser afterwards (e.g. the React app)."""
    return os.environ.get("PUBLISHER_UI_URL") or None


def output_roots() -> list[Path]:
    """Folders searched for `<source>/manifest.json` (backend job outputs, then CLI outputs)."""
    extra = [Path(p) for p in os.environ.get("PUBLISHER_OUTPUT_DIRS", "").split(os.pathsep) if p]
    base = Path(os.environ.get("CLIPPER_DATA", REPO_ROOT / "backend" / "data"))
    return extra + [base / "output", REPO_ROOT / "output"]


def env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""
