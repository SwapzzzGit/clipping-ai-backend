"""Background worker: moves jobs through transcript -> selection -> download -> render.

One job runs at a time (rendering already uses every core). Claude-mode jobs park in
`awaiting_claude` after the transcript step and resume once selection.json appears.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app import REPO_ROOT, claude_bridge
from app.store import BRAND_DIR, OUTPUT_DIR, UPLOADS_DIR, WORK_DIR, JobStore, Settings, now
from clipper.action import find_action_picks
from clipper.candidates import find_clips, normalize_external_picks, words_text
from clipper.config import load_config
from clipper.download import BOT_BLOCK_HELP, download_clips, is_bot_block
from clipper.branding import clip_branding
from clipper.music import clip_music
from clipper.llm import LLM
from clipper.local import find_upload, get_local_transcript, is_upload, load_upload_info, upload_id_from_url
from clipper.pipeline import llm_info, render_clips, write_outputs
from clipper.transcript import get_transcript, load_or_fetch_info
from clipper.util import ClipperError, find_ffmpeg, log as clipper_log, read_json

POLL_SEC = 1.5
PERIODIC_SEC = 10   # inbox scan and other housekeeping


def speech_word_count(transcript: dict) -> int:
    """Words the transcript actually has (word timings, or the words of each caption line)."""
    return sum(len(s["words"]) or len(s["text"].split()) for s in transcript["segments"])


class Cancelled(Exception):
    pass


class JobLogHandler(logging.Handler):
    """Routes the clipper library's log lines into the job currently being processed."""

    def __init__(self, worker: "Worker"):
        super().__init__(logging.INFO)
        self.worker = worker

    def emit(self, record):
        job_id = self.worker.current_job
        if job_id:
            self.worker.store.log(job_id, record.levelname.lower(), record.getMessage())


class Worker:
    def __init__(self, store: JobStore, settings: Settings):
        self.store = store
        self.settings = settings
        self.current_job: str | None = None
        self._stop = threading.Event()
        self._dispatching: set[str] = set()
        self._dlock = threading.Lock()
        self.done_hooks: list = []       # fn(job_id) after a job renders successfully
        self.periodic_hooks: list = []   # fn() every PERIODIC_SEC
        clipper_log.setLevel(logging.INFO)
        clipper_log.addHandler(JobLogHandler(self))

    # -- lifecycle ----------------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._loop, name="clipper-worker", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        last_periodic = 0.0
        while not self._stop.is_set():
            try:
                self._collect_claude_selections()
                if time.time() - last_periodic >= PERIODIC_SEC:
                    last_periodic = time.time()
                    for hook in self.periodic_hooks:
                        self._safe(hook)
                job = self.store.next_queued()
                if job:
                    self._run(job["id"])
                    if self.store.get(job["id"])["status"] == "done":
                        for hook in self.done_hooks:
                            self._safe(hook, job["id"])
                    continue
            except Exception:  # never let the worker thread die
                logging.getLogger("clipper.worker").exception("worker loop error")
            self._stop.wait(POLL_SEC)

    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            logging.getLogger("clipper.worker").exception("hook %s failed", getattr(fn, "__name__", fn))

    # -- config -------------------------------------------------------------------------

    def config_for(self, job: dict) -> dict:
        o = job["options"]
        auto = o["num_clips"] is None   # "Auto": the AI decides how many clips, up to auto_max
        overrides = {
            "work_dir": str(WORK_DIR),
            "output_dir": str(OUTPUT_DIR),
            "num_clips": o.get("auto_max", 5) if auto else o["num_clips"],
            "auto_clips": auto,
            "language": o["language"],
            "clips": {"min_sec": o["min_sec"], "max_sec": o["max_sec"]},
            "render": {"captions": o["captions"]},
        }
        return load_config(REPO_ROOT / "config.yaml", overrides)

    # -- pipeline -----------------------------------------------------------------------

    def _run(self, job_id: str):
        self.current_job = job_id
        self.store.update(job_id, status="running", error=None)
        try:
            job = self.store.get(job_id)
            cfg = self.config_for(job)
            ffmpeg = find_ffmpeg(cfg.get("ffmpeg_path"), WORK_DIR)
            local_src = None
            if is_upload(job["url"]):
                upload_id = upload_id_from_url(job["url"])
                local_src = find_upload(UPLOADS_DIR, upload_id)
                if not local_src:
                    raise ClipperError("The uploaded video is missing. Upload it again.")
                self._progress(job_id, "transcript", message="Transcribing the uploaded audio (no YouTube captions)")
                info, work = load_upload_info(UPLOADS_DIR, WORK_DIR, upload_id)
                transcript = get_local_transcript(info, work, local_src, cfg["language"], cfg.get("transcribe"),
                                                  ffmpeg=ffmpeg)
            else:
                self._progress(job_id, "transcript", message="Reading captions (no video download)")
                info, work = load_or_fetch_info(job["url"], WORK_DIR)
                transcript = get_transcript(info, work, cfg["language"])
            self.store.update(job_id, video={
                "id": info["id"], "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
                "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
                "captions": transcript["source"], "heatmap": bool(transcript["heatmap"]),
            })
            self._check_cancel(job_id)

            if local_src is not None and speech_word_count(transcript) < 3:
                # Nothing to read (gaming footage, music): pick the moments from the picture and the sound.
                cfg["render"]["captions"] = False
                job = self.store.get(job_id)
                if job["picks"] is None:
                    self._progress(job_id, "selection", message="No speech here: picking the action from the picture and sound")
                    picks = find_action_picks(local_src, cfg, ffmpeg, info.get("duration") or 0)
                    clips = normalize_external_picks(picks, transcript, cfg)
                    if not clips:
                        raise ClipperError("No action moments were found in this video.")
                    self.store.update(job_id, picks=clips, selection={"by": "action", "at": now()})
                self._check_cancel(job_id)

            job = self.store.get(job_id)
            if job["picks"] is None:
                if job["mode"] == "claude":
                    self._hand_to_claude(job, info, transcript, cfg)
                    return
                self._progress(job_id, "selection", message="Scoring the transcript with the LLM")
                llm = LLM(cfg["llm"], overrides=self.settings.llm_overrides())
                clips = find_clips(transcript, work, llm, cfg, job["options"]["rules"])
                self.store.update(job_id, picks=clips,
                                  selection={"by": "api", **llm_info(llm), "at": now()})
            self._check_cancel(job_id)

            job = self.store.get(job_id)
            clips = job["picks"]
            self._progress(job_id, "download", 0, len(clips), "Downloading only the chosen ranges")
            raws = download_clips(job["url"], clips, work, cfg, ffmpeg, info.get("duration") or 0,
                                  local_src=local_src)
            self._check_cancel(job_id)
            if raws and all(r.get("blocked") for r in raws.values()):
                raise ClipperError(BOT_BLOCK_HELP)  # nothing could be fetched: say why, at the top of the job

            opts = job["options"]
            for c in clips:
                c["text"] = words_text(transcript["segments"], c["start_sec"], c["end_sec"])  # the cleaned transcript
                c["music"] = clip_music(c, opts.get("music"), opts.get("music_volume", 20))
                c["branding"] = clip_branding(opts.get("branding"), c.get("branding_override"), BRAND_DIR, work,
                                              cfg["render"]["width"], cfg["render"]["height"])
                if c["branding"]:
                    self.store.log(job_id, "info", f"Clip {c['id']}: branding {sorted(k for k, v in c['branding'].items() if v)}")
                if c["music"]:
                    self.store.log(job_id, "info", f"Clip {c['id']}: background music {c['music']['name']}")
            self._progress(job_id, "render", 0, len(clips), "Face tracking, captions, 9:16 encode")
            out_dir = OUTPUT_DIR / job_id
            results = render_clips(clips, raws, work, out_dir, transcript["segments"], cfg, ffmpeg,
                                   redo_render=bool(job.get("force_render")),
                                   on_clip=lambda d, t: self._progress(job_id, "render", d, t,
                                                                       f"Rendered {d} of {t}"))
            sel = job["selection"] or {}
            manifest = write_outputs(out_dir, info, transcript, clips, results,
                                     {"model": sel.get("model", sel.get("by")), "usage": sel.get("usage")},
                                     cfg, job["options"]["rules"], job["url"])
            self._finish(job_id, manifest["clips"], len(clips))
        except Cancelled:
            self.store.update(job_id, status="cancelled", progress={"done": 0, "total": 0, "message": "Cancelled"})
        except ClipperError as e:
            blocked = is_bot_block(str(e))  # YouTube's bot check: the person can continue with an uploaded file
            message = BOT_BLOCK_HELP if blocked else str(e)
            self.store.log(job_id, "error", message)
            self.store.update(job_id, status="failed", error=message, blocked=blocked, finished_at=now())
        except Exception as e:
            logging.getLogger("clipper.worker").exception("job %s crashed", job_id)
            self.store.update(job_id, status="failed", error=f"Unexpected error: {e}", blocked=False, finished_at=now())
        finally:
            self.current_job = None

    def _finish(self, job_id: str, clips: list[dict], total: int) -> None:
        """Store the new manifest clips, keeping reviews as they are now (people review during a run)."""
        def apply(job):
            reviews = {c["id"]: c.get("review") for c in job["clips"]}
            for c in clips:
                c["review"] = reviews.get(c["id"]) or c["review"]
            ok = sum(1 for c in clips if c["status"] == "ok")
            job.update(clips=clips, stage="done", status="done" if ok else "failed", force_render=False,
                       error=None if ok else "No clip could be rendered; see the log.",
                       progress={"done": ok, "total": total, "message": f"{ok} clips ready"},
                       finished_at=now())

        self.store.mutate(job_id, apply)

    def _progress(self, job_id, stage, done=0, total=0, message=""):
        self.store.update(job_id, stage=stage, progress={"done": done, "total": total, "message": message})

    def _check_cancel(self, job_id):
        if self.store.get(job_id)["status"] == "cancelling":
            raise Cancelled()

    # -- Claude handoff -----------------------------------------------------------------

    def _hand_to_claude(self, job, info, transcript, cfg):
        d = claude_bridge.write_task(job, info, transcript, cfg, job["options"]["rules"])
        self.store.log(job["id"], "info", f"Selection handed to Claude: {d}")
        self.store.update(job["id"], status="awaiting_claude", stage="selection",
                          claude={**(job.get("claude") or {}), "task_dir": str(d), "created_at": now()},
                          progress={"done": 0, "total": 0, "message": "Waiting for Claude to choose the clips"})
        if self.settings.get()["claude_auto_dispatch"]:
            self.dispatch_claude(job["id"])

    def dispatch_claude(self, job_id: str) -> str:
        """Launch Claude Code headless for one job in a background thread."""
        s = self.settings.get()
        cmd = claude_bridge.find_claude(s["claude_command"])
        if not cmd:
            msg = "Claude Code CLI not found. Set its path in Settings, or run /clip-jobs in a Claude session."
            self.store.log(job_id, "warning", msg)
            self._set_claude(job_id, dispatch_status="unavailable", dispatch_log=msg)
            return "unavailable"
        with self._dlock:
            if job_id in self._dispatching:
                return "already_running"
            self._dispatching.add(job_id)
        self.store.log(job_id, "info", "Launched Claude Code to choose the clips")
        self._set_claude(job_id, dispatch_status="running", dispatched_at=now(), dispatch_log="")

        def run():
            try:
                ok, out = claude_bridge.run_claude(job_id, cmd, s["claude_model"], int(s["claude_timeout_min"]))
                self.store.log(job_id, "info" if ok else "warning",
                               "Claude finished" if ok else "Claude run did not produce a selection")
                self._set_claude(job_id, dispatch_status="done" if ok else "failed", dispatch_log=out)
            finally:
                with self._dlock:
                    self._dispatching.discard(job_id)

        threading.Thread(target=run, name=f"claude-{job_id}", daemon=True).start()
        return "started"

    def _set_claude(self, job_id, **fields):
        def apply(job):
            job["claude"] = {**(job.get("claude") or {}), **fields}
        self.store.mutate(job_id, apply)

    def _collect_claude_selections(self):
        for job in self.store.with_status("awaiting_claude"):
            path = claude_bridge.task_dir(job["id"]) / "selection.json"
            if path.is_file():
                self.ingest_selection(job["id"], path)

    def ingest_selection(self, job_id: str, path: Path) -> None:
        job = self.store.get(job_id)
        self.current_job = job_id
        try:
            try:
                data = read_json(path)
            except (json.JSONDecodeError, OSError) as e:
                raise ClipperError(f"selection.json is not valid JSON: {e}")
            picks = data.get("clips") if isinstance(data, dict) else data
            if not isinstance(picks, list) or not picks:
                raise ClipperError('selection.json must contain {"clips": [...]}')
            cfg = self.config_for(job)
            vid = (job.get("video") or {}).get("id")
            transcript = read_json(WORK_DIR / vid / "transcript.json")
            clips = normalize_external_picks(picks, transcript, cfg)
            if not clips:
                raise ClipperError("None of Claude's picks were valid clips (check times and lengths).")
            self.store.log(job_id, "info", f"Claude chose {len(clips)} clips; continuing mechanically")
            self.store.update(job_id, picks=clips, status="queued", stage="download",
                              selection={"by": "claude", "model": "claude", "usage": None, "at": now()})
        except ClipperError as e:
            bad = path.with_name(f"selection.rejected.{int(time.time())}.json")
            path.rename(bad)
            self.store.log(job_id, "error", f"{e} (moved to {bad.name}; still waiting for Claude)")
            self._set_claude(job_id, dispatch_status="failed", dispatch_log=str(e))
        finally:
            self.current_job = None
