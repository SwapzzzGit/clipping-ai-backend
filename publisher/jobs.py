"""File-backed publish job store (one JSON per job, like backend/app/store.py's JobStore).

A job fans one clip set out to many accounts: every (clip x account) is a *target* with its own
status: scheduled -> queued -> uploading -> processing -> published | failed | cancelled.
"""

from __future__ import annotations

import copy
import secrets
import threading
from pathlib import Path

from clipper.util import read_json, write_json
from publisher.models import TERMINAL, now

MAX_LOG = 300


class PublishJobStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        for f in self.dir.glob("*.json"):
            job = read_json(f)
            for t in job["targets"]:
                if t["status"] == "uploading":   # interrupted by a restart: resume (state keeps the session)
                    t["status"] = "queued"
            self._jobs[job["id"]] = job

    def create(self, source: str, targets: list[dict], scheduled_at: str | None, max_attempts: int) -> dict:
        job = {"id": secrets.token_hex(5), "source": source, "scheduled_at": scheduled_at, "status": "queued",
               "created_at": now(), "updated_at": now(), "targets": [], "log": []}
        for i, t in enumerate(targets, 1):
            when = t.get("scheduled_at") or scheduled_at
            job["targets"].append({
                "id": f"t{i}", "clip_id": t["clip_id"], "clip_title": t.get("clip_title"),
                "clip_file": t["clip_file"], "duration_sec": t.get("duration_sec"),
                "account_id": t["account_id"], "account_name": t.get("account_name"), "platform": t["platform"],
                "post": t["post"], "scheduled_at": when, "status": "scheduled" if when else "queued",
                "attempts": 0, "max_attempts": max_attempts, "next_attempt_at": 0.0, "progress": 0.0,
                "error": None, "message": "", "remote_id": None, "url": None,
                "state": {}, "result_state": None, "started_at": None, "finished_at": None})
        job["status"] = aggregate(job)
        with self._lock:
            self._jobs[job["id"]] = job
            self._save(job)
        return copy.deepcopy(job)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return copy.deepcopy(j) if j else None

    def list(self) -> list[dict]:
        with self._lock:
            jobs = [copy.deepcopy(j) for j in self._jobs.values()]
        return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    def update_target(self, job_id: str, target_id: str, persist: bool = True, **fields) -> dict:
        with self._lock:
            job = self._jobs[job_id]
            t = next(t for t in job["targets"] if t["id"] == target_id)
            t.update(fields)
            job["status"] = aggregate(job)
            job["updated_at"] = now()
            if persist:
                self._save(job)
            return copy.deepcopy(t)

    def target(self, job_id: str, target_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(next(t for t in self._jobs[job_id]["targets"] if t["id"] == target_id))

    def mutate(self, job_id: str, fn) -> dict:
        with self._lock:
            job = self._jobs[job_id]
            fn(job)
            job["status"] = aggregate(job)
            job["updated_at"] = now()
            self._save(job)
            return copy.deepcopy(job)

    def log(self, job_id: str, level: str, msg: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["log"].append({"t": now(), "level": level, "msg": msg})
            del job["log"][:-MAX_LOG]
            self._save(job)

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            (self.dir / f"{job_id}.json").unlink(missing_ok=True)

    def active_targets(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [(j["id"], copy.deepcopy(t)) for j in self._jobs.values()
                    for t in j["targets"] if t["status"] not in TERMINAL]

    def _save(self, job: dict) -> None:
        write_json(self.dir / f"{job['id']}.json", job)


def aggregate(job: dict) -> str:
    """Job-level status from its targets."""
    states = [t["status"] for t in job["targets"]]
    if not states:
        return "failed"
    if all(s == "published" for s in states):
        return "published"
    if all(s in TERMINAL for s in states):
        if all(s == "cancelled" for s in states):
            return "cancelled"
        return "partial" if "published" in states else "failed"
    if all(s == "scheduled" for s in states):
        return "scheduled"
    if any(s in ("uploading", "processing") for s in states) or any(s in TERMINAL for s in states):
        return "running"
    return "queued"
