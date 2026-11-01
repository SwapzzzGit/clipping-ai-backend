"""Clipper Studio API.

Run:  python backend/run.py   (serves the API on :8000 and, once built, the React app)
"""

from __future__ import annotations

import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import REPO_ROOT, auth, catalog, claude_bridge, trims
from app.store import OUTPUT_DIR, WORK_DIR, JobStore, Settings
from app.worker import Worker
from publisher.api import build_router as build_publish_router, install_tunnel_guard
from publisher.service import Publisher
from clipper.config import load_campaign_rules
from clipper.llm import LLM
from clipper.transcript import video_id_from_url
from clipper.util import ClipperError, find_ffmpeg, js_runtime, mmss, read_json

store = JobStore()
settings = Settings()
worker = Worker(store, settings)
categories = catalog.Categories()
publisher = Publisher(llm_factory=lambda: LLM({"max_tokens": 2048}, overrides=settings.llm_overrides()))
autopub = catalog.AutoPublisher(store, categories, publisher)
worker.done_hooks.append(autopub.on_rendered)
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


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True}


# -- models -------------------------------------------------------------------------------

class JobIn(BaseModel):
    url: str
    mode: Literal["claude", "api"] | None = None
    num_clips: int = Field(5, ge=1, le=20)
    min_sec: float = Field(20, ge=5, le=170)
    max_sec: float = Field(60, ge=10, le=180)
    language: str = "en"
    rules: str | None = None   # None = the category's rules, else campaign_rules.txt
    captions: bool = True
    category_id: str | None = None

    @field_validator("url")
    @classmethod
    def youtube_only(cls, v: str) -> str:
        v = v.strip()
        if not video_id_from_url(v):
            raise ValueError("Enter a YouTube video link (youtube.com/watch?v=…, youtu.be/…, or /shorts/…)")
        return v


class ReviewIn(BaseModel):
    approved: bool | None = None
    notes: str = ""


class MarkIn(BaseModel):
    kind: Literal["start", "end"]
    t: float


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
    approval: Literal["auto", "manual"] | None = None
    autopublish: bool | None = None
    min_score: float | None = Field(None, ge=0, le=10)
    max_clips_per_video: int | None = Field(None, ge=1, le=20)
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
    num_clips: int = Field(5, ge=1, le=20)
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


@app.post("/api/jobs", status_code=201)
def create_job(body: JobIn):
    if body.min_sec >= body.max_sec:
        raise HTTPException(422, "Minimum length must be shorter than maximum length")
    cat = categories.get(body.category_id) if body.category_id else None
    if body.category_id and not cat:
        raise HTTPException(422, f"Unknown category {body.category_id!r}")
    return _new_job(body.url, body.mode, body.model_dump(exclude={"url", "mode", "category_id"}), cat)


def _new_job(url: str, mode: str | None, options: dict, cat: dict | None, batch_id: str | None = None) -> dict:
    rules = options.get("rules")
    if rules is None:
        rules = (cat or {}).get("rules") or load_campaign_rules(REPO_ROOT / "campaign_rules.txt")
    options = {"language": "en", "captions": True, **options, "rules": rules.strip()}
    return store.create(url, mode or settings.get()["default_mode"], options,
                        category_id=cat["id"] if cat else None, batch_id=batch_id)


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
    return store.update(job_id, **fields)


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


@app.patch("/api/jobs/{job_id}/clips/{clip_id}")
def review_clip(job_id: str, clip_id: int, body: ReviewIn):
    _job_or_404(job_id)

    def apply(job):
        for c in job["clips"]:
            if c["id"] == clip_id:
                c["review"] = {**(c.get("review") or {}), "approved": body.approved, "notes": body.notes}
                return
        raise HTTPException(404, "Clip not found")

    store.mutate(job_id, apply)
    autopub.on_review(job_id, clip_id, body.approved)   # manual-approval categories publish on approve
    return store.get(job_id)


@app.post("/api/jobs/{job_id}/publish")
def publish_job(job_id: str, body: PublishIn):
    """Publish approved clips now (or at scheduled_at) to the category's accounts or chosen accounts."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        raise HTTPException(409, "Clips are not rendered yet")
    if body.account_ids is None and not job.get("category_id"):
        raise HTTPException(422, "This job has no category: choose accounts to publish to")
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
    options = {"num_clips": body.num_clips, "min_sec": body.min_sec, "max_sec": body.max_sec, "captions": body.captions}
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
    return _trim_mutate(job_id, lambda job, segs: trims.add_mark(job, clip_id, body.kind, body.t, segs))


@app.delete("/api/jobs/{job_id}/clips/{clip_id}/marks")
def clear_marks(job_id: str, clip_id: int):
    return _trim_mutate(job_id, lambda job, segs: trims.clear_marks(job, clip_id))


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
