"""File-backed job store and settings. One JSON file per job, so Claude and humans can read them."""

from __future__ import annotations

import datetime as dt
import os
import threading
import uuid
from pathlib import Path

from app import REPO_ROOT
from clipper.util import read_json, write_json

DATA_DIR = Path(os.environ.get("CLIPPER_DATA", REPO_ROOT / "backend" / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
WORK_DIR = DATA_DIR / "work"
OUTPUT_DIR = DATA_DIR / "output"
TASKS_DIR = DATA_DIR / "claude_tasks"
SETTINGS_PATH = DATA_DIR / "settings.json"

STAGES = ["transcript", "selection", "download", "render", "done"]
MAX_LOG = 500


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        for d in (JOBS_DIR, WORK_DIR, OUTPUT_DIR, TASKS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        for f in JOBS_DIR.glob("*.json"):
            job = read_json(f)
            if job.get("status") == "running":  # interrupted by a restart: resume it
                job["status"] = "queued"
            self._jobs[job["id"]] = job

    def create(self, url: str, mode: str, options: dict, category_id: str | None = None,
               batch_id: str | None = None) -> dict:
        job = {
            "id": uuid.uuid4().hex[:10],
            "url": url,
            "mode": mode,
            "category_id": category_id,
            "batch_id": batch_id,
            "options": options,
            "status": "queued",
            "stage": "transcript",
            "progress": {"done": 0, "total": 0, "message": "Waiting in queue"},
            "video": None,
            "selection": None,
            "picks": None,
            "claude": None,
            "clips": [],
            "error": None,
            "log": [],
            "created_at": now(),
            "updated_at": now(),
            "finished_at": None,
        }
        with self._lock:
            self._jobs[job["id"]] = job
            self._save(job)
        return job

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return _copy(job) if job else None

    def list(self) -> list[dict]:
        with self._lock:
            jobs = [_copy(j) for j in self._jobs.values()]
        return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    def update(self, job_id: str, **fields) -> dict:
        with self._lock:
            job = self._jobs[job_id]
            job.update(fields)
            job["updated_at"] = now()
            self._save(job)
            return _copy(job)

    def mutate(self, job_id: str, fn) -> dict:
        """Apply `fn(job)` under the lock (for nested edits such as a clip's review)."""
        with self._lock:
            job = self._jobs[job_id]
            fn(job)
            job["updated_at"] = now()
            self._save(job)
            return _copy(job)

    def log(self, job_id: str, level: str, msg: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["log"].append({"t": now(), "level": level, "msg": msg})
            del job["log"][:-MAX_LOG]
            job["updated_at"] = now()
            self._save(job)

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            (JOBS_DIR / f"{job_id}.json").unlink(missing_ok=True)

    def next_queued(self) -> dict | None:
        with self._lock:
            queued = [j for j in self._jobs.values() if j["status"] == "queued"]
            return _copy(min(queued, key=lambda j: j["created_at"])) if queued else None

    def with_status(self, status: str) -> list[dict]:
        with self._lock:
            return [_copy(j) for j in self._jobs.values() if j["status"] == status]

    def _save(self, job: dict) -> None:
        write_json(JOBS_DIR / f"{job['id']}.json", job)


def _copy(job: dict) -> dict:
    import copy

    return copy.deepcopy(job)


# -- settings ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    # "claude" | "api"; deployed servers have no Claude Code CLI, so set CLIPPER_DEFAULT_MODE=api there
    "default_mode": os.environ.get("CLIPPER_DEFAULT_MODE", "claude"),
    "llm_provider": "gemini",          # used in "api" mode
    "llm_model": "",                   # "" = provider default
    "llm_base_url": "",
    "llm_api_key": "",                 # stored locally in backend/data/settings.json, never returned
    "claude_auto_dispatch": True,      # launch Claude Code headless when a job needs a selection
    "claude_command": "",              # "" = auto-detect
    "claude_model": "",                # "" = Claude Code default
    "claude_timeout_min": 20,
}


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {**DEFAULT_SETTINGS, **(read_json(SETTINGS_PATH) if SETTINGS_PATH.is_file() else {})}

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict) -> dict:
        with self._lock:
            for k, v in changes.items():
                if k in DEFAULT_SETTINGS:
                    self._data[k] = v
            write_json(SETTINGS_PATH, self._data)
            return dict(self._data)

    def public(self) -> dict:
        """Settings safe to send to the browser: the API key is reduced to a hint."""
        s = self.get()
        key = s.pop("llm_api_key")
        s["has_api_key"] = bool(key)
        s["api_key_hint"] = f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else "")
        return s

    def llm_overrides(self) -> dict:
        s = self.get()
        return {"provider": s["llm_provider"], "model": s["llm_model"], "base_url": s["llm_base_url"],
                "api_key": s["llm_api_key"]}
