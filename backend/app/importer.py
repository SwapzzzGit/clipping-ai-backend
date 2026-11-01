"""Import clips rendered somewhere else (e.g. a laptop, where YouTube downloads work) as a finished job.

Hosted servers are usually blocked by YouTube ("Sign in to confirm you're not a bot"), so the
pipeline can run locally and only the results go up for review and publishing:

    POST /api/import/jobs                 {"manifest": {...}}  -> {"job_id", "files": [names to upload]}
    PUT  /api/import/jobs/{id}/files/{name}   raw file bytes (mp4 / jpg / ass)
    POST /api/import/jobs/{id}/finish     -> the job, now "done" with its clips

`backend/push_clips.py` drives this from the command line.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.store import OUTPUT_DIR, JobStore, now
from clipper.util import write_json

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(?:mp4|jpg|jpeg|png|ass)$")
MAX_FILE_BYTES = 300 * 1024 * 1024


class ImportIn(BaseModel):
    manifest: dict


def wanted_files(manifest: dict) -> list[str]:
    names = []
    for c in manifest.get("clips", []):
        if c.get("status", "ok") != "ok":
            continue
        for key in ("file", "thumbnail"):
            n = c.get(key)
            if n and SAFE_NAME.match(n) and n not in names:
                names.append(n)
    return names


def build_router(store: JobStore) -> APIRouter:
    r = APIRouter(prefix="/api/import", tags=["import"])

    @r.post("/jobs", status_code=201)
    def start(body: ImportIn):
        m = body.manifest
        clips = [c for c in m.get("clips", []) if c.get("status", "ok") == "ok" and c.get("file")]
        if not clips:
            raise HTTPException(422, "The manifest has no rendered clips")
        bad = [c.get("file") for c in clips if not SAFE_NAME.match(c["file"])]
        if bad:
            raise HTTPException(422, f"Unsafe file names: {bad}")
        video = m.get("video") or {}
        settings = m.get("settings") or {}
        options = {"num_clips": len(clips), "min_sec": (settings.get("clips") or {}).get("min_sec", 20),
                   "max_sec": (settings.get("clips") or {}).get("max_sec", 60), "language": "en",
                   "rules": m.get("campaign_rules") or "", "captions": True}
        job = store.create(video.get("url") or "", "claude", options)
        store.update(job["id"], status="importing", stage="render",
                     progress={"done": 0, "total": len(clips), "message": "Receiving clips rendered elsewhere"},
                     video={"id": video.get("id"), "title": video.get("title"), "channel": video.get("channel"),
                            "duration": video.get("duration_sec"), "thumbnail": None, "captions": None,
                            "heatmap": False},
                     imported={"manifest": m, "at": now()})
        (OUTPUT_DIR / job["id"]).mkdir(parents=True, exist_ok=True)
        store.log(job["id"], "info", f"Import started: {len(clips)} clips rendered elsewhere")
        return {"job_id": job["id"], "files": wanted_files(m)}

    @r.put("/jobs/{job_id}/files/{name}")
    async def upload(job_id: str, name: str, request: Request):
        job = store.get(job_id)
        if not job or job["status"] != "importing":
            raise HTTPException(404, "No import in progress for this job")
        if name not in wanted_files(job["imported"]["manifest"]):
            raise HTTPException(422, f"{name} is not part of this import")
        dest = OUTPUT_DIR / job_id / name
        tmp = dest.with_suffix(dest.suffix + ".part")
        size = 0
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, "File too large")
                f.write(chunk)
        tmp.replace(dest)
        return {"file": name, "bytes": size}

    @r.post("/jobs/{job_id}/finish")
    def finish(job_id: str):
        job = store.get(job_id)
        if not job or job["status"] != "importing":
            raise HTTPException(404, "No import in progress for this job")
        m = job["imported"]["manifest"]
        missing = [n for n in wanted_files(m) if not (OUTPUT_DIR / job_id / n).is_file()]
        if missing:
            raise HTTPException(409, f"Still missing: {', '.join(missing)}")
        clips = [c for c in m["clips"] if c.get("status", "ok") == "ok"]
        for c in clips:
            c.setdefault("review", {"approved": None, "notes": ""})
        write_json(OUTPUT_DIR / job_id / "manifest.json", {**m, "clips": clips})
        store.log(job_id, "info", f"Imported {len(clips)} clips")
        return store.update(job_id, status="done", stage="done", clips=clips, picks=None,
                            selection={"by": "claude", "model": "claude", "usage": None, "at": now(),
                                       "imported": True},
                            imported={"at": job["imported"]["at"], "finished_at": now()},
                            progress={"done": len(clips), "total": len(clips), "message": f"{len(clips)} clips ready"},
                            finished_at=now())

    return r
