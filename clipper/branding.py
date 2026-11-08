"""Branding for clips: a logo and a progress bar drawn over the finished picture.

Brand images live in a folder the app passes in (backend/data/branding), and the defaults are
saved in brand.json beside them. A job takes a copy of the defaults when it is created, so
changing the brand later does not touch clips that are already rendering. A clip can override
its logo position and switch the progress bar on or off.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import cv2
import numpy as np

from clipper.util import ClipperError, read_json, write_json

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 10 * 1024 * 1024
POSITIONS = ("top-left", "top-center", "top-right", "middle-left", "middle-center", "middle-right",
             "bottom-left", "bottom-center", "bottom-right")
BAR_STYLES = ("solid", "gradient", "image")
BAR_EDGES = ("top", "bottom")
BAR_HEIGHT = 0.009   # share of the frame height
EDGE_MARGIN = 0.03   # share of the frame width kept clear around the logo
HEX = re.compile(r"#[0-9A-Fa-f]{6}")

DEFAULTS = {
    "logo": None,
    "logo_position": "top-right",
    "logo_size": 18,     # % of the frame width
    "logo_opacity": 90,  # %
    "progress": {"enabled": False, "style": "solid", "color": "#FFD400", "color2": "#FF3D7F",
                 "image": None, "edge": "bottom"},
}


# -- brand images -----------------------------------------------------------------------------

def list_assets(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size}
            for p in sorted(folder.iterdir(), key=lambda p: p.name.lower())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS]


def asset_path(folder: Path, name: str) -> Path:
    """An uploaded image by its file name. Only a bare file name is trusted, never a path."""
    path = folder / Path(name or "").name
    if not name or path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
        raise ClipperError(f"Branding image {name!r} is not uploaded.")
    return path


def save_asset(folder: Path, filename: str, data: bytes) -> dict:
    ext = Path(filename or "").suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ClipperError("Upload a PNG, JPG or WebP image.")
    if not data:
        raise ClipperError("The uploaded image was empty.")
    if len(data) > MAX_BYTES:
        raise ClipperError("Images must be 10 MB or smaller.")
    if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED) is None:
        raise ClipperError("That file is not a readable image.")
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename).stem).strip(" ._") or "image"
    dest = folder / f"{stem}{ext}"
    if dest.exists():  # keep the older file: the new one gets a short hash in its name
        dest = folder / f"{stem}-{hashlib.sha1(data).hexdigest()[:6]}{ext}"
    dest.write_bytes(data)
    return {"name": dest.name, "bytes": len(data)}


def delete_asset(folder: Path, name: str) -> None:
    asset_path(folder, name).unlink()


# -- defaults ---------------------------------------------------------------------------------

def _merged(data: dict) -> dict:
    d = copy.deepcopy(DEFAULTS)
    d.update({k: v for k, v in data.items() if k in DEFAULTS and k != "progress"})
    d["progress"].update((data.get("progress") or {}))
    return d


def check_defaults(folder: Path, data: dict) -> dict:
    d = _merged(data)
    if d["logo"]:
        asset_path(folder, d["logo"])
    if d["logo_position"] not in POSITIONS:
        raise ClipperError("Pick one of the nine logo positions.")
    if not 5 <= int(d["logo_size"]) <= 50:
        raise ClipperError("The logo size must be between 5% and 50% of the width.")
    if not 10 <= int(d["logo_opacity"]) <= 100:
        raise ClipperError("The logo opacity must be between 10% and 100%.")
    d["logo_size"], d["logo_opacity"] = int(d["logo_size"]), int(d["logo_opacity"])
    p = d["progress"]
    if p["style"] not in BAR_STYLES or p["edge"] not in BAR_EDGES:
        raise ClipperError("Pick a progress bar style and edge.")
    for key in ("color", "color2"):
        if not HEX.fullmatch(p[key] or ""):
            raise ClipperError("Colours must be hex codes like #FFD400.")
    if p["style"] == "image":
        if not p["image"]:
            raise ClipperError("Upload an image for the progress bar, or pick a colour.")
        asset_path(folder, p["image"])
    p["enabled"] = bool(p["enabled"])
    return d


def load_defaults(folder: Path) -> dict:
    path = folder / "brand.json"
    d = _merged(read_json(path) if path.is_file() else {})
    if d["logo"] and not (folder / Path(d["logo"]).name).is_file():
        d["logo"] = None  # the file was deleted outside the app
    if d["progress"]["image"] and not (folder / Path(d["progress"]["image"]).name).is_file():
        d["progress"]["image"] = None
    return d


def save_defaults(folder: Path, data: dict) -> dict:
    d = check_defaults(folder, data)
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "brand.json", d)
    return d


# -- what one clip draws ----------------------------------------------------------------------

def logo_xy(position: str, margin: int) -> tuple[str, str]:
    """Overlay x and y for a logo position such as "top-right"."""
    row, col = position.split("-")
    x = {"left": str(margin), "center": "(main_w-overlay_w)/2", "right": f"main_w-overlay_w-{margin}"}[col]
    y = {"top": str(margin), "middle": "(main_h-overlay_h)/2", "bottom": f"main_h-overlay_h-{margin}"}[row]
    return x, y


def _bgr(hex_code: str) -> np.ndarray:
    h = hex_code.lstrip("#")
    return np.array([int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)], np.float32)


def _bgra(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return img


def make_bar(style: str, color: str, color2: str, image: Path | None, width: int, height: int,
             out_dir: Path) -> Path:
    """A width x height PNG strip for the progress bar. The renderer reveals it over the clip's length."""
    stamp = image.stat().st_mtime_ns if image else 0
    key = hashlib.sha1(json.dumps([style, color, color2, str(image), stamp, width, height]).encode()).hexdigest()[:16]
    out = out_dir / f"bar_{key}.png"
    if out.is_file():
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    if style == "image":
        src = cv2.imdecode(np.fromfile(str(image), np.uint8), cv2.IMREAD_UNCHANGED)
        if src is None:
            raise ClipperError("The progress bar image could not be read. Upload it again.")
        src = _bgra(src)
        scale = max(width / src.shape[1], height / src.shape[0])  # cover the bar, then crop the middle
        fit = cv2.resize(src, (round(src.shape[1] * scale), round(src.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        y0, x0 = (fit.shape[0] - height) // 2, (fit.shape[1] - width) // 2
        strip = fit[y0:y0 + height, x0:x0 + width]
    else:
        if style == "gradient":
            t = np.linspace(0, 1, width, dtype=np.float32)[None, :, None]
            row = _bgr(color) * (1 - t) + _bgr(color2) * t
        else:
            row = _bgr(color)[None, None, :]
        rgb = np.broadcast_to(row, (height, width, 3)).astype(np.uint8)
        strip = np.dstack([rgb, np.full((height, width), 255, np.uint8)])
    cv2.imwrite(str(out), strip)
    return out


def clip_branding(job_branding: dict | None, override: dict | None, folder: Path, work: Path,
                  width: int, height: int) -> dict | None:
    """What one clip draws: {"logo": {...} or None, "progress": {...} or None}, or None for a plain clip."""
    override = override or {}
    base = job_branding or copy.deepcopy(DEFAULTS)
    position = override.get("logo_position", base["logo_position"])
    logo = None
    if position != "none" and base.get("logo") and (folder / Path(base["logo"]).name).is_file():
        path = folder / Path(base["logo"]).name
        logo = {"path": str(path), "position": position, "size": base["logo_size"],
                "opacity": base["logo_opacity"], "stamp": path.stat().st_mtime_ns}
    progress = None
    if override.get("progress", base["progress"]["enabled"]):
        p = base["progress"]
        image = folder / Path(p["image"]).name if p.get("image") else None
        bar = make_bar(p["style"], p["color"], p["color2"], image, width, round(height * BAR_HEIGHT),
                       work / "branding")
        progress = {"path": str(bar), "edge": p["edge"]}
    return {"logo": logo, "progress": progress} if logo or progress else None
