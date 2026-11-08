"""Clipper Studio API.

Run:  python backend/run.py   (serves the API on :8000 and, once built, the React app)
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

from app import REPO_ROOT, auth, catalog, claude_bridge, importer, trims
from app.store import BRAND_DIR, OUTPUT_DIR, UPLOADS_DIR, WORK_DIR, JobStore, Settings, now
from app.worker import Worker
from publisher.api import build_router as build_publish_router, install_tunnel_guard
from publisher.service import Publisher
from clipper.config import load_campaign_rules
from clipper.download import is_bot_block
from clipper.llm import LLM
from clipper.local import META_SUFFIX, UPLOAD_EXTENSIONS, find_upload, is_upload
from clipper.branding import (MAX_BYTES, POSITIONS, asset_path, delete_asset, list_assets, load_defaults,
                              save_asset, save_defaults)
from clipper.music import list_tracks as list_music_tracks, resolve as resolve_music
from clipper.candidates import words_between
from clipper.captions import word_key
from clipper.pycaps_captions import DEFAULT_TEMPLATE, OURS, PREVIEW_DIR, is_valid, style_options
from clipper.cuts import probe_duration, waveform
from clipper.strip import filmstrip
from clipper.transcript import video_id_from_url
from clipper.util import ClipperError, find_ffmpeg, js_runtime, mmss, read_json, video_fps, write_json

store = JobStore()
settings = Settings()
worker = Worker(store, settings)
categories = catalog.Categories()
publisher = Publisher(llm_factory=lambda: LLM({"max_tokens": 2048}, overrides=settings.llm_overrides()))
autopub = catalog.AutoPublisher(store, categories, publisher)
worker.periodic_hooks.append(lambda: catalog.ingest_inbox(_create_batch))


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker.start()
    publisher.start()
    yield
    publisher.stop()
    worker.stop()


app = FastAPI(title="Clipper Studio", version="0.1.0", lifespan=lifespan)
_origins = ["http://localhost:5173", "http://127.0.0.1:5173"] + [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
auth.install(app)        # APP_PASSWORD set = sign-in required (deployed); unset = open (local dev)
app.include_router(auth.router)
app.include_router(importer.build_router(store))   # clips rendered on a laptop, pushed by backend/push_clips.py


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True}


# -- models -------------------------------------------------------------------------------

class JobIn(BaseModel):
    url: str
    mode: Literal["claude", "api"] | None = None
    num_clips: int | None = Field(None, ge=1, le=20)   # None = Auto: the AI decides, up to auto_max
    auto_max: int = Field(5, ge=1, le=20)
    min_sec: float = Field(20, ge=5, le=170)
    max_sec: float = Field(60, ge=10, le=180)
    language: str = "en"
    rules: str | None = None   # None = the category's rules, else campaign_rules.txt
    captions: bool = True
    category_id: str | None = None
    music: str | None = None           # None = no music, "auto" = match each clip's mood, else a library file
    music_volume: int = Field(20, ge=0, le=100)
    use_branding: bool = True          # a copy of the Branding defaults is taken when the job is created

    @field_validator("url")
    @classmethod
    def youtube_or_upload(cls, v: str) -> str:
        v = v.strip()
        if is_upload(v):
            if not re.fullmatch(r"upload:[0-9a-f]{12}", v):
                raise ValueError("That upload is not valid. Upload the video again.")
            return v
        if not video_id_from_url(v):
            raise ValueError("Enter a YouTube video link (youtube.com/watch?v=…, youtu.be/…, or /shorts/…)")
        return v


class ReviewIn(BaseModel):
    approved: bool | None = None
    notes: str = ""


class MarkIn(BaseModel):
    kind: Literal["start", "end"]
    t: float
    snap: bool = True   # True moves the mark to the nearest word edge; False keeps the exact frame


class CutIn(BaseModel):
    s: float   # source seconds where the first cut word starts
    e: float   # source seconds where the last cut word ends
    text: str = ""


class CutRestoreIn(BaseModel):
    s: float
    e: float


class SelectionIn(BaseModel):
    clips: list[dict]


class RetryIn(BaseModel):
    from_stage: Literal["transcript", "selection", "download", "render"] = "download"


class SettingsIn(BaseModel):
    default_mode: Literal["claude", "api"] | None = None
    llm_provider: Literal["gemini", "anthropic", "openai", "mock"] | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None      # only sent when the user types a new key
    clear_api_key: bool = False
    claude_auto_dispatch: bool | None = None
    claude_command: str | None = None
    claude_model: str | None = None
    claude_timeout_min: int | None = Field(None, ge=1, le=120)


class CategoryIn(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    account_ids: list[str] | None = None
    approval: Literal["manual"] | None = None   # a person approves every clip; there is no automatic mode
    spacing_minutes: int | None = Field(None, ge=0, le=7 * 24 * 60)
    hashtags: list[str] | None = None
    use_llm_captions: bool | None = None
    rules: str | None = None


class BatchItem(BaseModel):
    url: str
    category: str | None = None      # category id or name


class BatchIn(BaseModel):
    items: list[BatchItem] | None = None
    text: str | None = None          # or: one "url[,category]" per line
    default_category: str | None = None
    mode: Literal["claude", "api"] | None = None
    num_clips: int | None = Field(None, ge=1, le=20)   # None = Auto
    auto_max: int = Field(5, ge=1, le=20)
    min_sec: float = Field(20, ge=5, le=170)
    max_sec: float = Field(60, ge=10, le=180)
    captions: bool = True


class PublishIn(BaseModel):
    clip_ids: list[int] | None = None       # default: every approved clip
    account_ids: list[str] | None = None    # default: the job's category accounts
    scheduled_at: str | None = None


def _job_or_404(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _summary(job: dict) -> dict:
    s = {k: v for k, v in job.items() if k not in ("log", "picks")}
    s["clip_count"] = len(job["clips"]) or len(job["picks"] or [])
    return s


# -- jobs ---------------------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs():
    return [_summary(j) for j in store.list()]


MAX_UPLOAD_BYTES = 20 * 1024**3
UPLOAD_BLOCK = 8 * 1024 * 1024   # written in 8 MB blocks: fast even for multi-GB files


@app.post("/api/uploads", status_code=201)
async def upload_video(request: Request, name: str = Query(..., min_length=1, max_length=200)):
    """Stream a video from this PC to disk. The body is the raw file (no multipart parsing)."""
    ext = Path(name).suffix.lower()
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(422, "Use an MP4, MOV, M4V, MKV, WebM or AVI video.")
    upload_id = uuid.uuid4().hex[:12]
    dest = UPLOADS_DIR / f"{upload_id}{ext}"
    written = 0
    try:
        with open(dest, "wb", buffering=UPLOAD_BLOCK) as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "That file is larger than 20 GB.")
                f.write(chunk)
        if written == 0:
            raise HTTPException(422, "The upload was empty.")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    write_json(UPLOADS_DIR / f"{upload_id}{META_SUFFIX}", {"name": name, "size": written, "uploaded_at": now()})
    return {"id": upload_id, "name": name, "size": written, "url": f"upload:{upload_id}"}


@app.get("/api/music")
def music_library():
    return {"tracks": list_music_tracks()}


# -- branding: logo and progress bar -----------------------------------------------------------

class ProgressIn(BaseModel):
    enabled: bool = False
    style: Literal["solid", "gradient", "image"] = "solid"
    color: str = "#FFD400"
    color2: str = "#FF3D7F"
    image: str | None = None
    edge: Literal["top", "bottom"] = "bottom"


class BrandIn(BaseModel):
    logo: str | None = None
    logo_position: str = "top-right"
    logo_size: int = Field(18, ge=5, le=50)
    logo_opacity: int = Field(90, ge=10, le=100)
    progress: ProgressIn = ProgressIn()


class BrandOverrideIn(BaseModel):
    logo_position: str | None = None   # one of the nine positions, or "none" to hide the logo on this clip
    progress: bool | None = None       # None keeps the job's choice


@app.get("/api/branding")
def get_branding():
    return {"assets": list_assets(BRAND_DIR), "defaults": load_defaults(BRAND_DIR), "positions": list(POSITIONS)}


@app.post("/api/branding/upload", status_code=201)
async def upload_brand_image(request: Request, name: str = Query(..., min_length=1, max_length=200)):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        raise HTTPException(422, "Images must be 10 MB or smaller.")
    try:
        return save_asset(BRAND_DIR, name, await request.body())
    except ClipperError as e:
        raise HTTPException(422, str(e))


@app.get("/api/branding/files/{name}")
def brand_image(name: str):
    try:
        return FileResponse(asset_path(BRAND_DIR, name))
    except ClipperError:
        raise HTTPException(404, "Not found")


@app.delete("/api/branding/files/{name}", status_code=204)
def delete_brand_image(name: str):
    defaults = load_defaults(BRAND_DIR)
    if name in (defaults["logo"], defaults["progress"]["image"]):
        raise HTTPException(409, "That image is your logo or progress bar. Change the Branding defaults first.")
    try:
        delete_asset(BRAND_DIR, name)
    except ClipperError as e:
        raise HTTPException(404, str(e))


@app.put("/api/branding")
def save_branding(body: BrandIn):
    try:
        return save_defaults(BRAND_DIR, body.model_dump())
    except ClipperError as e:
        raise HTTPException(422, str(e))


CaptionFont = Literal["Arial", "Arial Black", "Impact", "Verdana", "Tahoma", "Trebuchet MS", "Segoe UI", "Georgia"]


class CaptionStyleIn(BaseModel):
    enabled: bool = True
    font: CaptionFont = "Arial"
    size: int = Field(84, ge=24, le=160)
    bold: bool = True
    animation: Literal["words", "lines"] = "words"
    max_chars: int = Field(0, ge=0, le=60)
    highlight: Literal["yellow", "white", "cyan", "green", "orange", "pink"] = "yellow"
    shadow: int = Field(3, ge=0, le=8)


@app.put("/api/jobs/{job_id}/clips/{clip_id}/captions")
def clip_captions(job_id: str, clip_id: int, body: CaptionStyleIn):
    """Save one clip's caption style, then re-render just that clip."""
    _job_or_404(job_id)

    def apply(job):
        if job["status"] in trims.BUSY:
            raise trims.TrimError(f"Job is {job['status']}", 409)
        pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), None)
        if pick is None:
            raise trims.TrimError("Clip not found", 404)
        pick["caption_style"] = body.model_dump()
        for c in job.get("clips") or []:
            if c["id"] == clip_id:
                c["rerendering"] = True
        job.update(status="queued", stage="download", error=None, finished_at=None,
                   progress={"done": 0, "total": 0, "message": f"Re-rendering clip {clip_id} with new captions"})

    try:
        return store.mutate(job_id, apply)
    except trims.TrimError as e:
        raise HTTPException(e.status, str(e))


@app.delete("/api/jobs/{job_id}/clips/{clip_id}/captions")
def clip_captions_reset(job_id: str, clip_id: int):
    """Use the job's caption settings for this clip again, and re-render it if it had its own."""
    _job_or_404(job_id)

    def apply(job):
        pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), None)
        if pick is None:
            raise trims.TrimError("Clip not found", 404)
        if "caption_style" not in pick:
            return  # already the job's settings: nothing to re-render
        if job["status"] in trims.BUSY:
            raise trims.TrimError(f"Job is {job['status']}", 409)
        del pick["caption_style"]
        for c in job.get("clips") or []:
            if c["id"] == clip_id:
                c["rerendering"] = True
        job.update(status="queued", stage="download", error=None, finished_at=None,
                   progress={"done": 0, "total": 0, "message": f"Re-rendering clip {clip_id} with the job's captions"})

    try:
        return store.mutate(job_id, apply)
    except trims.TrimError as e:
        raise HTTPException(e.status, str(e))


class CaptionTextIn(BaseModel):
    edits: dict[str, str] = {}

    @field_validator("edits")
    @classmethod
    def _valid_edits(cls, edits: dict[str, str]) -> dict[str, str]:
        if len(edits) > 500:
            raise ValueError("Up to 500 caption words can be corrected")
        clean = {}
        for key, text in edits.items():
            if not key.replace(".", "", 1).isdigit():
                raise ValueError(f"{key!r} is not a caption word")
            text = " ".join(text.split())
            if len(text) > 60:
                raise ValueError("A caption word can be up to 60 characters")
            clean[key] = text
        return clean


@app.put("/api/jobs/{job_id}/clips/{clip_id}/caption-text")
def clip_caption_text(job_id: str, clip_id: int, body: CaptionTextIn):
    """Save the corrected caption words of one clip, then re-render just that clip."""
    _job_or_404(job_id)

    def apply(job):
        pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), None)
        if pick is None:
            raise trims.TrimError("Clip not found", 404)
        if (pick.get("caption_text") or {}) == body.edits:
            return  # nothing changed, so nothing is re-rendered
        if job["status"] in trims.BUSY:
            raise trims.TrimError(f"Job is {job['status']}", 409)
        if body.edits:
            pick["caption_text"] = body.edits
        else:
            pick.pop("caption_text", None)
        for c in job.get("clips") or []:
            if c["id"] == clip_id:
                c["rerendering"] = True
        job.update(status="queued", stage="download", error=None, finished_at=None,
                   progress={"done": 0, "total": 0, "message": f"Re-rendering clip {clip_id} with corrected captions"})

    try:
        return store.mutate(job_id, apply)
    except trims.TrimError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/caption-styles")
def caption_styles():
    """Every caption style a clip can use: ours (the ASS captions) and the pycaps templates, with previews."""
    return {"default": DEFAULT_TEMPLATE, "styles": style_options()}


@app.get("/api/caption-previews/{name}")
def caption_preview(name: str):
    """A still frame that shows one caption style (made by scripts/make_caption_previews.py)."""
    path = PREVIEW_DIR / f"{name}.jpg"
    if not is_valid(name) or name == OURS or not path.is_file():
        raise HTTPException(404, "No preview for that caption style")
    return FileResponse(path, media_type="image/jpeg")


class CaptionTemplateIn(BaseModel):
    template: str = Field(min_length=1, max_length=40)


@app.put("/api/jobs/{job_id}/clips/{clip_id}/caption-template")
def clip_caption_template_set(job_id: str, clip_id: int, body: CaptionTemplateIn):
    """Use another caption style for one clip, then re-render just that clip."""
    _job_or_404(job_id)
    if not is_valid(body.template):
        raise HTTPException(422, f"Unknown caption style: {body.template}")

    def apply(job):
        pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), None)
        if pick is None:
            raise trims.TrimError("Clip not found", 404)
        if pick.get("caption_template") == body.template:
            return  # already this style: nothing to re-render
        if job["status"] in trims.BUSY:
            raise trims.TrimError(f"Job is {job['status']}", 409)
        pick["caption_template"] = body.template
        for c in job.get("clips") or []:
            if c["id"] == clip_id:
                c["rerendering"] = True
        job.update(status="queued", stage="download", error=None, finished_at=None,
                   progress={"done": 0, "total": 0, "message": f"Re-rendering clip {clip_id} with the {body.template} captions"})

    try:
        return store.mutate(job_id, apply)
    except trims.TrimError as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/jobs/{job_id}/clips/{clip_id}/branding")
def clip_branding_override(job_id: str, clip_id: int, body: BrandOverrideIn):
    """Move one clip's logo or switch its progress bar, then re-render just that clip."""
    if body.logo_position not in (None, "none", *POSITIONS):
        raise HTTPException(422, "Pick one of the nine logo positions, or none.")
    _job_or_404(job_id)

    def apply(job):
        if job["status"] in trims.BUSY:
            raise trims.TrimError(f"Job is {job['status']}", 409)
        pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), None)
        if pick is None:
            raise trims.TrimError("Clip not found", 404)
        override = dict(pick.get("branding_override") or {})
        if body.logo_position is not None:
            override["logo_position"] = body.logo_position
        if body.progress is not None:
            override["progress"] = body.progress
        pick["branding_override"] = override
        for c in job.get("clips") or []:
            if c["id"] == clip_id:
                c["rerendering"] = True
        job.update(status="queued", stage="download", error=None, finished_at=None,
                   progress={"done": 0, "total": 0, "message": f"Re-rendering clip {clip_id} with new branding"})

    try:
        return store.mutate(job_id, apply)
    except trims.TrimError as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/jobs", status_code=201)
def create_job(body: JobIn):
    if is_upload(body.url) and not find_upload(UPLOADS_DIR, body.url.removeprefix("upload:")):
        raise HTTPException(422, "The uploaded video is missing. Upload it again.")
    if body.min_sec >= body.max_sec:
        raise HTTPException(422, "Minimum length must be shorter than maximum length")
    if body.music and body.music != "auto":
        try:
            resolve_music(body.music)
        except ClipperError as e:
            raise HTTPException(422, str(e))
    cat = categories.get(body.category_id) if body.category_id else None
    if body.category_id and not cat:
        raise HTTPException(422, f"Unknown category {body.category_id!r}")
    options = body.model_dump(exclude={"url", "mode", "category_id", "use_branding"})
    options["branding"] = load_defaults(BRAND_DIR) if body.use_branding else None
    return _new_job(body.url, body.mode, options, cat)


def _new_job(url: str, mode: str | None, options: dict, cat: dict | None, batch_id: str | None = None) -> dict:
    rules = options.get("rules")
    if rules is None:
        rules = (cat or {}).get("rules") or load_campaign_rules(REPO_ROOT / "campaign_rules.txt")
    options = {"language": "en", "captions": True, **options, "rules": rules.strip()}
    return store.create(url, mode or settings.get()["default_mode"], options,
                        category_id=cat["id"] if cat else None, batch_id=batch_id)


@app.get("/api/jobs/{job_id}/clips.zip")
def download_all_clips(job_id: str):
    """Every rendered clip of a job in one zip: the Download all button."""
    job = _job_or_404(job_id)
    job_dir = OUTPUT_DIR / job_id
    files = [job_dir / c["file"] for c in job.get("clips") or [] if c.get("file")]
    files = [f for f in files if f.is_file()]
    if not files:
        raise HTTPException(404, "No rendered clips yet")
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=job_dir)
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:  # the clips are compressed video already
        for f in files:
            z.write(f, arcname=f.name)
    title = " ".join(re.sub(r"[^\w .-]+", " ", (job.get("video") or {}).get("title") or "").split())[:60]
    return FileResponse(tmp, media_type="application/zip", filename=f"{title or job_id} clips.zip",
                        background=BackgroundTask(os.remove, tmp))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job_or_404(job_id)


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    job = _job_or_404(job_id)
    if job["status"] == "running":
        raise HTTPException(409, "Cancel the job before deleting it")
    store.delete(job_id)
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    shutil.rmtree(claude_bridge.task_dir(job_id), ignore_errors=True)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _job_or_404(job_id)
    if job["status"] == "running":
        return store.update(job_id, status="cancelling")
    if job["status"] in ("queued", "awaiting_claude"):
        return store.update(job_id, status="cancelled")
    raise HTTPException(409, f"Job is {job['status']}")


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, body: RetryIn):
    job = _job_or_404(job_id)
    if job["status"] in ("running", "queued", "cancelling"):
        raise HTTPException(409, f"Job is {job['status']}")
    fields = {"status": "queued", "error": None, "finished_at": None}
    if body.from_stage in ("transcript", "selection"):
        fields |= {"picks": None, "selection": None, "clips": []}
    if body.from_stage in ("download", "render"):
        fields["force_render"] = True  # re-render every clip, not only the ones whose settings changed
    return store.update(job_id, **fields)


class SourceIn(BaseModel):
    upload_url: str                  # "upload:<id>" from POST /api/uploads
    rights_confirmed: bool


@app.post("/api/jobs/{job_id}/source")
def replace_source(job_id: str, body: SourceIn):
    """YouTube blocked the download. Continue this job with a video the person downloaded and uploaded."""
    job = _job_or_404(job_id)
    blocked = job.get("blocked") or is_bot_block(job.get("error") or "")
    if job["status"] != "failed" or not blocked or is_upload(job["url"]):
        raise HTTPException(409, "Only a job that YouTube blocked can continue with an uploaded video")
    if not body.rights_confirmed:
        raise HTTPException(422, "Confirm that you have the rights to use this video")
    if not is_upload(body.upload_url) or not find_upload(UPLOADS_DIR, body.upload_url.removeprefix("upload:")):
        raise HTTPException(422, "The uploaded video is missing. Upload it again.")
    # The moments were picked from YouTube's captions, so they are picked again from the file that was uploaded.
    return store.update(job_id, url=body.upload_url, original_url=job["url"], status="queued", error=None,
                        blocked=False, finished_at=None, picks=None, selection=None, clips=[], force_render=False,
                        rights_confirmed_at=now())


@app.post("/api/jobs/{job_id}/mode")
def switch_mode(job_id: str, mode: Literal["claude", "api"]):
    """Switch a job that is waiting on Claude over to the API path (or back)."""
    job = _job_or_404(job_id)
    if job["status"] not in ("awaiting_claude", "failed", "cancelled"):
        raise HTTPException(409, f"Job is {job['status']}")
    return store.update(job_id, mode=mode, status="queued", picks=None, selection=None, error=None)


@app.post("/api/jobs/{job_id}/dispatch-claude")
def dispatch_claude(job_id: str):
    job = _job_or_404(job_id)
    if job["status"] != "awaiting_claude":
        raise HTTPException(409, "This job is not waiting for Claude")
    result = worker.dispatch_claude(job_id)
    if result == "unavailable":
        raise HTTPException(503, "Claude Code CLI not found. Set its path in Settings, or run /clip-jobs in Claude Code.")
    return {"result": result}


@app.post("/api/jobs/{job_id}/selection")
def submit_selection(job_id: str, body: SelectionIn):
    """Clips chosen by Claude (or a person) over HTTP; the worker validates and continues."""
    job = _job_or_404(job_id)
    if job["status"] != "awaiting_claude":
        raise HTTPException(409, "This job is not waiting for a selection")
    claude_bridge.save_selection(job_id, body.model_dump())
    return {"result": "accepted"}


class TitleIn(BaseModel):
    title: str


@app.patch("/api/jobs/{job_id}/clips/{clip_id}/title")
def rename_clip(job_id: str, clip_id: int, body: TitleIn):
    """The name a person gives a clip. It is the name shown in the editor and used when posting."""
    title = body.title.strip()[:120]
    if not title:
        raise HTTPException(422, "Give the clip a name")
    _job_or_404(job_id)

    def apply(job):
        for c in job["clips"]:
            if c["id"] == clip_id:
                c["title"] = title
                return
        raise HTTPException(404, "Clip not found")

    store.mutate(job_id, apply)
    return store.get(job_id)


@app.patch("/api/jobs/{job_id}/clips/{clip_id}")
def review_clip(job_id: str, clip_id: int, body: ReviewIn):
    _job_or_404(job_id)

    def apply(job):
        for c in job["clips"]:
            if c["id"] == clip_id:
                # Recorded so the publish gate can tell a person's approval from anything else.
                c["review"] = {**(c.get("review") or {}), "approved": body.approved, "notes": body.notes,
                               "approved_by": "person" if body.approved else None}
                return
        raise HTTPException(404, "Clip not found")

    store.mutate(job_id, apply)
    return store.get(job_id)


MAX_EDITED_BYTES = 8 * 1024**3


@app.post("/api/jobs/{job_id}/clips/{clip_id}/edited")
async def upload_edited_clip(job_id: str, clip_id: int, request: Request):
    """clipper: the export from the full editor becomes this clip's video. The rendered file stays on disk."""
    job = _job_or_404(job_id)
    if job["status"] in trims.BUSY:
        raise HTTPException(409, f"Job is {job['status']}")
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if clip is None:
        raise HTTPException(404, "Clip not found")
    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Name the file after the clip. An edit of an edit keeps the original stem instead of stacking suffixes.
    stem = Path(clip.get("file") or f"{clip_id:02d}_clip.mp4").stem.split("_edited")[0]
    dest = out_dir / f"{stem}_edited_{uuid.uuid4().hex[:6]}.mp4"
    written = 0
    try:
        with open(dest, "wb", buffering=UPLOAD_BLOCK) as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_EDITED_BYTES:
                    raise HTTPException(413, "That export is larger than 8 GB.")
                f.write(chunk)
        if written == 0:
            raise HTTPException(422, "The export was empty.")
        try:
            seconds = probe_duration(find_ffmpeg(None, WORK_DIR), dest)
        except Exception:
            raise HTTPException(422, "That file is not a video this editor can play.")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    def apply(j):
        for c in j["clips"]:
            if c["id"] == clip_id:
                c["file"] = dest.name
                c["duration_sec"] = round(seconds, 2)
                c["edited_in_editor"] = True
                c["rerendering"] = False
                # The file changed, so the earlier approval no longer covers it.
                c["review"] = {**(c.get("review") or {}), "approved": None, "approved_by": None}
                return
        raise HTTPException(404, "Clip not found")

    store.mutate(job_id, apply)
    return store.get(job_id)


@app.post("/api/jobs/{job_id}/publish")
def publish_job(job_id: str, body: PublishIn):
    """Publish approved clips now (or at scheduled_at) to the accounts the person chose."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        raise HTTPException(409, "Clips are not rendered yet")
    if not body.account_ids:
        raise HTTPException(422, "Choose at least one account to post to")
    pjob = autopub.publish_approved(job_id, body.clip_ids, body.account_ids, "manual", body.scheduled_at)
    if not pjob:
        raise HTTPException(409, "Nothing new to publish: approve clips and connect accounts first "
                                 "(clips already queued for an account are skipped)")
    return pjob


# -- categories, batches, posts ------------------------------------------------------------

@app.get("/api/categories")
def list_categories():
    accounts = {a["id"]: a for a in publisher.accounts.list()}
    jobs = store.list()
    out = []
    for c in categories.list():
        c["accounts"] = [accounts[a] for a in c["account_ids"] if a in accounts]
        c["job_count"] = sum(1 for j in jobs if j.get("category_id") == c["id"])
        out.append(c)
    return out


@app.post("/api/categories", status_code=201)
def create_category(body: CategoryIn):
    try:
        return categories.create(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/api/categories/{cid}")
def update_category(cid: str, body: CategoryIn):
    try:
        return categories.update(cid, body.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(404, "Category not found")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/categories/{cid}", status_code=204)
def delete_category(cid: str):
    categories.delete(cid)


def _create_batch(items: list[dict], default_category: str | None = None, mode: str | None = None,
                  options: dict | None = None, source: str = "api") -> dict:
    accepted, errors = catalog.resolve_batch(items, categories, default_category)
    batch_id = "b-" + secrets.token_hex(4)
    jobs = [_new_job(it["url"], mode, dict(options or {}), categories.get(it["category_id"]), batch_id)
            for it in accepted]
    for j in jobs:
        store.log(j["id"], "info", f"Created by batch {batch_id} ({source})")
    return {"batch_id": batch_id, "jobs": [j["id"] for j in jobs], "errors": errors}


@app.post("/api/batches", status_code=201)
def create_batch(body: BatchIn):
    if body.min_sec >= body.max_sec:
        raise HTTPException(422, "Minimum length must be shorter than maximum length")
    items = [i.model_dump() for i in body.items or []] + catalog.parse_batch_lines(body.text or "")
    if not items:
        raise HTTPException(422, "Give at least one URL")
    if body.default_category and not categories.find(body.default_category):
        raise HTTPException(422, f"Unknown category {body.default_category!r}")
    options = {"num_clips": body.num_clips, "auto_max": body.auto_max, "min_sec": body.min_sec,
               "max_sec": body.max_sec, "captions": body.captions}
    result = _create_batch(items, body.default_category, body.mode, options)
    if not result["jobs"]:
        raise HTTPException(422, "No valid URLs: " + "; ".join(result["errors"]))
    return result


@app.get("/api/batches")
def list_batches():
    batches: dict[str, dict] = {}
    for j in store.list():
        if not j.get("batch_id"):
            continue
        b = batches.setdefault(j["batch_id"], {"id": j["batch_id"], "created_at": j["created_at"], "statuses": {}, "jobs": 0})
        b["jobs"] += 1
        b["statuses"][j["status"]] = b["statuses"].get(j["status"], 0) + 1
        b["created_at"] = min(b["created_at"], j["created_at"])
    return sorted(batches.values(), key=lambda b: b["created_at"], reverse=True)


@app.get("/api/posts")
def posts():
    return catalog.posts_overview(store, categories, publisher)


def _segments(job: dict) -> list[dict]:
    vid = (job.get("video") or {}).get("id")
    path = WORK_DIR / vid / "transcript.json" if vid else None
    if not path or not path.is_file():
        raise HTTPException(409, "This job has no transcript yet")
    return read_json(path)["segments"]


def _trim_mutate(job_id: str, fn) -> dict:
    """Run a trims.* edit under the store lock, turning TrimError into an HTTP error."""
    segments = _segments(_job_or_404(job_id))

    def apply(job):
        try:
            fn(job, segments)
        except trims.TrimError as e:
            raise HTTPException(e.status, str(e))

    return store.mutate(job_id, apply)


@app.post("/api/jobs/{job_id}/clips/{clip_id}/marks")
def add_mark(job_id: str, clip_id: int, body: MarkIn):
    return _trim_mutate(job_id, lambda job, segs: trims.add_mark(job, clip_id, body.kind, body.t, segs, body.snap))


@app.post("/api/jobs/{job_id}/clips/{clip_id}/cuts")
def add_cut(job_id: str, clip_id: int, body: CutIn):
    return _trim_mutate(job_id, lambda job, segs: trims.add_cut(job, clip_id, body.s, body.e, body.text))


@app.post("/api/jobs/{job_id}/clips/{clip_id}/cuts/restore")
def restore_cut(job_id: str, clip_id: int, body: CutRestoreIn):
    return _trim_mutate(job_id, lambda job, segs: trims.restore_cut(job, clip_id, body.s, body.e))


class FillersIn(BaseModel):
    on: bool
    words: list[str] = ["so"]


@app.post("/api/jobs/{job_id}/clips/{clip_id}/fillers")
def set_fillers(job_id: str, clip_id: int, body: FillersIn):
    """Cut the filler words the list names out of the clip, or put them back."""
    return _trim_mutate(job_id, lambda job, segs: trims.set_fillers(job, clip_id, segs, body.on, body.words))


@app.delete("/api/jobs/{job_id}/clips/{clip_id}/marks")
def clear_marks(job_id: str, clip_id: int):
    return _trim_mutate(job_id, lambda job, segs: trims.clear_marks(job, clip_id))


@app.get("/api/jobs/{job_id}/clips/{clip_id}/timing")
def clip_timing(job_id: str, clip_id: int):
    """Frame rate of a rendered clip, so the editor can show and step frames."""
    job = _job_or_404(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip or not clip.get("file"):
        raise HTTPException(404, "No rendered clip")
    return {"fps": video_fps(find_ffmpeg(None, WORK_DIR), OUTPUT_DIR / job_id / clip["file"])}


@app.get("/api/jobs/{job_id}/clips/{clip_id}/words")
def clip_words(job_id: str, clip_id: int):
    """The clip's transcript words in source seconds, for the text-based editor."""
    job = _job_or_404(job_id)
    pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), None)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not pick or not clip:
        raise HTTPException(404, "Clip not found")
    words = words_between(_segments(job), pick["start_sec"], pick["end_sec"])
    return {
        "words": [{"i": i, "k": word_key(w["start"]), "text": w["text"], "s": round(w["start"], 3), "e": round(w["end"], 3)}
                  for i, w in enumerate(words)],
        "source_t0": clip.get("source_t0"),
        "duration_sec": clip.get("duration_sec"),
    }


@app.get("/api/jobs/{job_id}/clips/{clip_id}/filmstrip")
def clip_filmstrip(job_id: str, clip_id: int):
    """Thumbnails of a rendered clip for the editor's video lane: the picture files are <stem>_strip_NNN.jpg."""
    job = _job_or_404(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip or not clip.get("file"):
        raise HTTPException(404, "No rendered clip")
    src = OUTPUT_DIR / job_id / clip["file"]
    meta = filmstrip(find_ffmpeg(None, WORK_DIR), src, float(clip.get("duration_sec") or 0))
    return {"stem": src.stem, **meta}


@app.get("/api/jobs/{job_id}/clips/{clip_id}/waveform")
def clip_waveform(job_id: str, clip_id: int):
    """Sound level of a rendered clip, for the editor's timeline."""
    job = _job_or_404(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip or not clip.get("file"):
        raise HTTPException(404, "No rendered clip")
    return waveform(find_ffmpeg(None, WORK_DIR), OUTPUT_DIR / job_id / clip["file"])


@app.post("/api/jobs/{job_id}/clips/{clip_id}/rerender")
def rerender_clip(job_id: str, clip_id: int):
    """Trim one clip to its marks and re-queue the job; cached steps skip every other clip."""
    ranges = {}

    def apply(job, segs):
        ranges["old"], ranges["new"] = trims.apply_trim(job, clip_id, segs)
        job.update(status="queued", stage="download", error=None, finished_at=None,
                   progress={"done": 0, "total": 0, "message": f"Re-rendering clip {clip_id}"})

    _trim_mutate(job_id, apply)
    (a, b), (c, d) = ranges["old"], ranges["new"]
    store.log(job_id, "info", f"Re-rendering clip {clip_id}: {mmss(a)}-{mmss(b)} -> {mmss(c)}-{mmss(d)}")
    return store.get(job_id)


# -- Claude queue ---------------------------------------------------------------------------

@app.get("/api/claude/tasks")
def claude_tasks():
    """Jobs waiting for Claude, with the folder holding each task."""
    return [{"job_id": j["id"], "title": (j.get("video") or {}).get("title"), "task_dir": (j.get("claude") or {}).get("task_dir"),
             "dispatch_status": (j.get("claude") or {}).get("dispatch_status")}
            for j in store.with_status("awaiting_claude")]


# -- settings & system --------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    changes = body.model_dump(exclude_none=True, exclude={"llm_api_key", "clear_api_key"})
    if body.clear_api_key:
        changes["llm_api_key"] = ""
    elif body.llm_api_key and body.llm_api_key.strip():
        changes["llm_api_key"] = body.llm_api_key.strip()
    settings.update(changes)
    return settings.public()


@app.post("/api/settings/test-llm")
def test_llm():
    try:
        llm = LLM({"max_tokens": 64, "timeout_sec": 30}, overrides=settings.llm_overrides())
        reply = llm.complete("Reply with the single word OK.", "Say OK.", fallback=lambda: "OK")
        return {"ok": True, "model": llm.name, "reply": reply.strip()[:80]}
    except ClipperError as e:
        return {"ok": False, "error": str(e)[:400]}


@app.get("/api/system")
def system():
    s = settings.get()
    cmd = claude_bridge.find_claude(s["claude_command"])
    try:
        ffmpeg = find_ffmpeg(None, WORK_DIR)
    except ClipperError:
        ffmpeg = None
    import os

    return {
        "ffmpeg": ffmpeg,
        "js_runtime": js_runtime(),
        "claude_cli": cmd,
        "claude_version": claude_bridge.claude_version(cmd) if cmd else None,
        "env_keys": {k: bool(os.environ.get(k)) for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY")},
        "data_dir": str(OUTPUT_DIR.parent),
        "disk_free_gb": round(shutil.disk_usage(OUTPUT_DIR.parent).free / 1e9, 1),
        "worker_busy": worker.current_job,
    }


# -- publishing (OAuth accounts, variants, publish jobs); before the SPA catch-all -----------

app.include_router(build_publish_router(publisher), prefix="/api/publish")
install_tunnel_guard(app, publisher)   # the free public link only exposes OAuth callbacks + clip links


# -- static: rendered clips + the built React app ---------------------------------------

app.mount("/media", StaticFiles(directory=OUTPUT_DIR), name="media")

DIST = REPO_ROOT / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
