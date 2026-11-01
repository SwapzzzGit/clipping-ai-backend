"""Categories, batch intake, autopublishing and the posts tracker.

A **category** (finance, motivation, ...) groups connected accounts from any platforms; one
account may sit in several categories. A clip job tagged with a category publishes to every
account in it. Per category you decide how autonomous that is:

    approval = "auto"    clips scoring >= min_score are approved and published as soon as they render
    approval = "manual"  nothing posts until a person approves a clip; approving then publishes it

Duplicate guard: a (job, clip, account) pair is only ever queued once, whatever path triggers it.

Batch intake: POST /api/batches with many URLs (each with its own category), or drop a .txt/.csv
file of `url[,category]` lines into backend/data/inbox/ and the worker ingests it.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import secrets
import threading
from pathlib import Path

from app.store import DATA_DIR, JobStore, now
from clipper.transcript import video_id_from_url
from clipper.util import log, read_json, write_json

CATEGORIES_PATH = DATA_DIR / "categories.json"
INBOX_DIR = DATA_DIR / "inbox"

CATEGORY_DEFAULTS = {
    "name": "",
    "description": "",
    "color": "#e0531f",
    "account_ids": [],
    "approval": "manual",        # "auto" | "manual"
    "autopublish": True,         # publish approved clips without a button press
    "min_score": 7.0,            # auto approval threshold (0-10)
    "max_clips_per_video": 3,    # auto approval publishes at most this many per video
    "spacing_minutes": 0,        # gap between successive clips of one video (0 = all at once)
    "hashtags": [],              # added to every post in this category
    "use_llm_captions": False,   # write per-platform copy with the API-key LLM
    "rules": "",                 # extra content rules for clip selection ("" = campaign_rules.txt)
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or secrets.token_hex(3)


class Categories:
    def __init__(self, path: Path = CATEGORIES_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        if path.is_file():
            for c in read_json(path):
                self._items[c["id"]] = {**CATEGORY_DEFAULTS, **c}

    def list(self) -> list[dict]:
        with self._lock:
            return sorted((dict(c) for c in self._items.values()), key=lambda c: c["name"].lower())

    def get(self, cid: str | None) -> dict | None:
        with self._lock:
            c = self._items.get(cid or "")
            return dict(c) if c else None

    def find(self, ref: str) -> dict | None:
        """By id or (case-insensitive) name, for batch files that say `finance` or `Finance`."""
        ref = ref.strip().lower()
        with self._lock:
            for c in self._items.values():
                if c["id"] == ref or c["name"].lower() == ref:
                    return dict(c)
        return None

    def create(self, data: dict) -> dict:
        name = data.get("name", "").strip()
        if not name:
            raise ValueError("A category needs a name")
        if self.find(name):
            raise ValueError(f"A category called {name!r} already exists")
        with self._lock:
            cid = _slug(name)
            while cid in self._items:
                cid = f"{cid}-{secrets.token_hex(2)}"
            item = {**CATEGORY_DEFAULTS, **_clean(data), "id": cid, "name": name, "created_at": now()}
            self._items[cid] = item
            self._save()
            return dict(item)

    def update(self, cid: str, data: dict) -> dict:
        with self._lock:
            if cid not in self._items:
                raise KeyError(cid)
            if "name" in data:
                other = self.find(data["name"])
                if other and other["id"] != cid:
                    raise ValueError(f"A category called {data['name']!r} already exists")
            self._items[cid].update(_clean(data))
            self._save()
            return dict(self._items[cid])

    def delete(self, cid: str) -> None:
        with self._lock:
            self._items.pop(cid, None)
            self._save()

    def drop_account(self, account_id: str) -> None:
        """An account was disconnected: take it out of every category."""
        with self._lock:
            for c in self._items.values():
                if account_id in c["account_ids"]:
                    c["account_ids"] = [a for a in c["account_ids"] if a != account_id]
            self._save()

    def _save(self) -> None:
        write_json(self.path, list(self._items.values()))


def _clean(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k in CATEGORY_DEFAULTS and v is not None}
    if "hashtags" in out:
        tags = [t.strip() for t in out["hashtags"] if t and t.strip()]
        out["hashtags"] = [t if t.startswith("#") else f"#{t}" for t in tags]
    if "account_ids" in out:
        out["account_ids"] = list(dict.fromkeys(out["account_ids"]))
    if "approval" in out and out["approval"] not in ("auto", "manual"):
        raise ValueError("approval must be 'auto' or 'manual'")
    return out


# -- batch intake -----------------------------------------------------------------------------

def parse_batch_lines(text: str) -> list[dict]:
    """`url[,category]` or `url<tab>category` or `url category` per line; '#' comments allowed."""
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader([line.replace("\t", ",")]))
        if len(parts) == 1 and " " in parts[0]:
            parts = parts[0].split(None, 1)
        url = parts[0].strip()
        if url.lower() == "url":   # CSV header
            continue
        items.append({"url": url, "category": (parts[1].strip() if len(parts) > 1 else None) or None})
    return items


def resolve_batch(items: list[dict], cats: Categories, default_category: str | None) -> tuple[list[dict], list[str]]:
    """Validate URLs and categories. Returns (accepted [{url, category_id}], errors)."""
    ok, errors = [], []
    for i, it in enumerate(items, 1):
        url = (it.get("url") or "").strip()
        if not video_id_from_url(url):
            errors.append(f"line {i}: not a YouTube video link: {url[:80]}")
            continue
        ref = it.get("category") or it.get("category_id") or default_category
        cat = cats.find(ref) if ref else None
        if ref and not cat:
            errors.append(f"line {i}: unknown category {ref!r}")
            continue
        ok.append({"url": url, "category_id": cat["id"] if cat else None})
    return ok, errors


def ingest_inbox(create_batch) -> list[str]:
    """Turn files dropped in data/inbox/ into batches. Returns the names of processed files."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    done_dir = INBOX_DIR / "processed"
    done = []
    for f in sorted(INBOX_DIR.glob("*")):
        if not f.is_file() or f.suffix.lower() not in (".txt", ".csv"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("inbox: cannot read %s: %s", f.name, e)
            continue
        result = create_batch(parse_batch_lines(text), source=f"inbox:{f.name}")
        done_dir.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        f.rename(done_dir / f"{stamp}-{f.name}")
        (done_dir / f"{stamp}-{f.name}.result.txt").write_text(
            f"batch {result['batch_id']}: {len(result['jobs'])} jobs\n" + "\n".join(result["errors"]), encoding="utf-8")
        done.append(f.name)
    return done


# -- autopublish ----------------------------------------------------------------------------------

class AutoPublisher:
    """Turns category rules into publish jobs, without ever queueing a (clip, account) pair twice."""

    def __init__(self, store: JobStore, cats: Categories, publisher):
        self.store = store
        self.cats = cats
        self.pub = publisher
        self._lock = threading.Lock()

    def on_rendered(self, job_id: str) -> None:
        """Called by the worker when a job finishes rendering."""
        job = self.store.get(job_id)
        cat = self.cats.get(job.get("category_id")) if job else None
        if not job or job["status"] != "done" or not cat:
            return
        if cat["approval"] == "auto":
            ok = [c for c in job["clips"] if c.get("status", "ok") == "ok" and c.get("score", 0) >= cat["min_score"]]
            chosen = sorted(ok, key=lambda c: c.get("score", 0), reverse=True)[: cat["max_clips_per_video"]]
            ids = {c["id"] for c in chosen}

            def approve(j):
                for c in j["clips"]:
                    if c["id"] in ids and (c.get("review") or {}).get("approved") is None:
                        c["review"] = {**(c.get("review") or {}), "approved": True, "approved_by": "auto"}
            self.store.mutate(job_id, approve)
            self.store.log(job_id, "info", f"Auto-approved {len(ids)} clip(s) scoring >= {cat['min_score']} "
                                           f"for category {cat['name']}")
        if cat["autopublish"]:
            self.publish_approved(job_id, reason="autopublish")

    def on_review(self, job_id: str, clip_id: int, approved: bool | None) -> None:
        job = self.store.get(job_id)
        cat = self.cats.get(job.get("category_id")) if job else None
        if approved and cat and cat["autopublish"]:
            self.publish_approved(job_id, clip_ids=[clip_id], reason="approved")

    def publish_approved(self, job_id: str, clip_ids: list[int] | None = None, account_ids: list[str] | None = None,
                         reason: str = "manual", scheduled_at: str | None = None) -> dict | None:
        """Publish approved clips of a job to its category's accounts (or `account_ids`)."""
        if self.pub is None:
            return None
        with self._lock:
            job = self.store.get(job_id)
            cat = self.cats.get(job.get("category_id"))
            accounts = account_ids if account_ids is not None else (cat["account_ids"] if cat else [])
            connected = {a["id"]: a for a in self.pub.accounts.list()}
            accounts = [a for a in accounts if a in connected and connected[a].get("status", "connected") == "connected"]
            clips = [c for c in job["clips"] if c.get("status", "ok") == "ok" and (c.get("review") or {}).get("approved")
                     and (clip_ids is None or c["id"] in clip_ids)]
            done_pairs = {(p["clip_id"], p["account_id"]) for p in (job.get("publish") or {}).get("pairs", [])}
            pairs = [(c, a) for c in sorted(clips, key=lambda c: c.get("score", 0), reverse=True) for a in accounts
                     if (c["id"], a) not in done_pairs]
            if not pairs:
                if clips and not accounts:
                    self.store.log(job_id, "warning", "Approved clips are ready, but the category has no connected accounts")
                return None
            try:
                variants = self.pub.preview(job_id, sorted({a for _, a in pairs}), sorted({c["id"] for c, _ in pairs}),
                                            use_llm=bool(cat and cat["use_llm_captions"]))
            except Exception as e:  # missing manifest, platform disabled, LLM failure
                self.store.log(job_id, "error", f"Could not prepare posts: {e}")
                return None
            by_pair = {(v["clip_id"], v["account_id"]): v for v in variants}
            spacing = (cat or {}).get("spacing_minutes", 0) if reason != "manual" else 0
            order = {cid: i for i, cid in enumerate(dict.fromkeys(c["id"] for c, _ in pairs))}
            base = _parse(scheduled_at) or dt.datetime.now(dt.timezone.utc)
            targets = []
            for c, a in pairs:
                v = by_pair.get((c["id"], a))
                if not v:
                    continue
                post = dict(v["post"])
                if cat and cat["hashtags"]:
                    post["hashtags"] = list(dict.fromkeys(cat["hashtags"] + post.get("hashtags", [])))
                when = None
                if scheduled_at or (spacing and order[c["id"]]):
                    when = (base + dt.timedelta(minutes=spacing * order[c["id"]])).isoformat(timespec="seconds")
                targets.append({"clip_id": c["id"], "account_id": a, "post": post, "scheduled_at": when})
            pjob = self.pub.create_job(job_id, targets=targets)

            def record(j):
                p = j.setdefault("publish", {"jobs": [], "pairs": []})
                p["jobs"].append(pjob["id"])
                p["pairs"] += [{"clip_id": t["clip_id"], "account_id": t["account_id"], "publish_job": pjob["id"]}
                               for t in targets]
            self.store.mutate(job_id, record)
            self.store.log(job_id, "info", f"Publishing {len({t['clip_id'] for t in targets})} clip(s) to "
                                           f"{len({t['account_id'] for t in targets})} account(s) ({reason})")
            return pjob


def _parse(s: str | None):
    if not s:
        return None
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# -- posts tracker ------------------------------------------------------------------------------

def posts_overview(store: JobStore, cats: Categories, publisher) -> dict:
    """Every clip x account the lifecycle knows about, including approved clips not yet posted."""
    accounts = {a["id"]: a for a in publisher.accounts.list()} if publisher else {}
    targets = {}
    if publisher:
        for pj in publisher.jobs.list():
            for t in pj["targets"]:
                targets[(pj["source"], t["clip_id"], t["account_id"])] = (pj, t)
    rows = []
    for job in store.list():
        if job["status"] != "done":
            continue
        cat = cats.get(job.get("category_id"))
        cat_accounts = cat["account_ids"] if cat else []
        for c in job["clips"]:
            if c.get("status", "ok") != "ok":
                continue
            review = (c.get("review") or {}).get("approved")
            seen = {k[2] for k in targets if k[0] == job["id"] and k[1] == c["id"]}
            for aid in list(dict.fromkeys(cat_accounts + sorted(seen))):
                hit = targets.get((job["id"], c["id"], aid))
                a = accounts.get(aid, {})
                if hit:
                    pj, t = hit
                    status, url, err, when = t["status"], t.get("url"), t.get("error"), t.get("finished_at") or t.get("scheduled_at")
                else:
                    pj, status, url, err, when = None, ("rejected" if review is False else
                                                        "not_posted" if review else "awaiting_review"), None, None, None
                rows.append({
                    "job_id": job["id"], "video_title": (job.get("video") or {}).get("title"),
                    "category_id": cat["id"] if cat else None, "category": cat["name"] if cat else None,
                    "clip_id": c["id"], "clip_title": c["title"], "score": c.get("score"), "file": c.get("file"),
                    "account_id": aid, "account": a.get("display_name") or a.get("username") or aid,
                    "platform": a.get("platform") or (t["platform"] if hit else None),
                    "status": status, "url": url, "error": err, "when": when,
                    "publish_job": pj["id"] if pj else None,
                })
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"rows": rows, "counts": counts}
