"""Orchestrates the four steps with per-video caching, then writes manifest.json + review.html."""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from clipper import __version__
from clipper.candidates import find_clips
from clipper.config import load_campaign_rules
from clipper.download import download_clips
from clipper.llm import LLM
from clipper.reframe import FaceDetector, analyze
from clipper.render import render_clip
from clipper.transcript import get_transcript, load_or_fetch_info
from clipper.util import ClipperError, find_ffmpeg, hms, log, mmss, read_json, slugify, write_json

STEPS = ["transcript", "candidates", "download", "render"]


def run(url: str, cfg: dict, rules_path: str | None, force_from: str | None = None,
        candidates_only: bool = False) -> Path:
    redo = set(STEPS[STEPS.index(force_from):]) if force_from else set()
    work_root, out_root = Path(cfg["work_dir"]), Path(cfg["output_dir"])
    ffmpeg = None if candidates_only else find_ffmpeg(cfg.get("ffmpeg_path"), work_root)

    # Step 1: metadata + transcript (no video download).
    info, work = load_or_fetch_info(url, work_root)
    vid = info["id"]
    log.info("video: %s (%s, %s)", info.get("title"), vid, mmss(info.get("duration") or 0))
    transcript = get_transcript(info, work, cfg["language"], force="transcript" in redo)

    # Step 2: LLM candidate search.
    llm = LLM(cfg["llm"])
    rules = load_campaign_rules(rules_path)
    if rules:
        log.info("campaign rules loaded (%d lines)", len(rules.splitlines()))
    clips = find_clips(transcript, work, llm, cfg, rules, force="candidates" in redo)
    write_json(work / "clips_public.json", [
        {k: c[k] for k in ("id", "start_sec", "end_sec", "score", "title", "hook_line", "reason")} for c in clips
    ])
    for c in clips:
        log.info("  #%d %s-%s score %.2f  %s", c["id"], mmss(c["start_sec"]), mmss(c["end_sec"]), c["score"], c["title"])

    out_dir = out_root / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    if candidates_only:
        manifest = build_manifest(info, transcript, clips, {}, llm_info(llm), cfg, rules, url, rendered=False)
        write_json(out_dir / "manifest.json", manifest)
        log.info("candidates only: wrote %s", out_dir / "manifest.json")
        return out_dir

    # Step 3: partial downloads.
    # Raw files are named by time range, so only an explicit --force download discards them.
    if force_from == "download":
        for f in (work / "raw").glob("*.mp4"):
            f.unlink()
    raws = download_clips(url, clips, work, cfg, ffmpeg, info.get("duration") or 0)

    # Step 4: reframe + captions + encode.
    results = render_clips(clips, raws, work, out_dir, transcript["segments"], cfg, ffmpeg,
                           redo_render="render" in redo, redo_analysis=force_from == "download")

    manifest = write_outputs(out_dir, info, transcript, clips, results, llm_info(llm), cfg, rules, url)
    ok = sum(1 for c in manifest["clips"] if c["status"] == "ok")
    log.info("done: %d/%d clips rendered -> %s", ok, len(clips), out_dir.resolve())
    log.info("LLM usage: %s", llm.usage)
    if ok == 0:
        raise ClipperError("No clips could be rendered; see errors above.")
    return out_dir


def llm_info(llm: LLM) -> dict:
    return {"model": llm.name, "usage": dict(llm.usage)}


def render_clips(clips: list[dict], raws: dict[int, dict], work: Path, out_dir: Path, segments: list[dict],
                 cfg: dict, ffmpeg: str, redo_render: bool = False, redo_analysis: bool = False,
                 on_clip=None) -> dict[int, dict]:
    """Face-track, caption and encode every downloaded clip. `on_clip(done, total)` reports progress."""
    results: dict[int, dict] = {}
    detector = None
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for n, c in enumerate(clips, 1):
            raw = raws[c["id"]]
            if raw.get("error"):
                results[c["id"]] = {"status": "failed", "error": f"download: {raw['error']}"}
                continue
            out = out_dir / f"{c['id']:02d}_{slugify(c['title'])}.mp4"
            analysis_path = work / "analysis" / (Path(raw["path"]).stem + ".json")
            try:
                if analysis_path.is_file() and not redo_analysis:
                    analysis = read_json(analysis_path)
                else:
                    detector = detector or FaceDetector(Path(cfg["work_dir"]) / ".models")
                    log.info("clip %d: face tracking", c["id"])
                    analysis = analyze(Path(raw["path"]), detector, cfg["render"]["face_sample_fps"],
                                       cfg["render"]["shot_change_threshold"])
                    write_json(analysis_path, analysis)
                meta_path = out.with_suffix(".render.json")
                key = {"raw": Path(raw["path"]).name, "start_sec": c["start_sec"], "end_sec": c["end_sec"],
                       "render": cfg["render"]}
                cached = read_json(meta_path) if meta_path.is_file() else {}
                if out.is_file() and cached.get("key") == key and not redo_render:
                    log.info("clip %d: already rendered", c["id"])
                    rendered = cached["result"]
                else:
                    log.info("clip %d: rendering %s", c["id"], out.name)
                    rendered = render_clip(raw, c, analysis, segments, out, cfg, ffmpeg)
                    write_json(meta_path, {"key": key, "result": rendered})
                results[c["id"]] = {"status": "ok", "file": out.name, "analysis": analysis, **rendered}
            except ClipperError as e:
                log.error("%s", e)
                results[c["id"]] = {"status": "failed", "error": str(e)}
            if on_clip:
                on_clip(n, len(clips))
    finally:
        if detector:
            detector.close()
    return results


def write_outputs(out_dir, info, transcript, clips, results, llm, cfg, rules, url) -> dict:
    """Prune stale files, then write manifest.json and review.html. Returns the manifest."""
    remove_stale_outputs(out_dir, {Path(r["file"]).stem for r in results.values() if r.get("file")})
    manifest = build_manifest(info, transcript, clips, results, llm, cfg, rules, url, rendered=True)
    write_json(out_dir / "manifest.json", manifest)
    (out_dir / "review.html").write_text(review_html(manifest), encoding="utf-8")
    return manifest


def remove_stale_outputs(out_dir: Path, keep_stems: set[str]) -> None:
    """Delete clips from earlier runs so the output folder matches manifest.json exactly."""
    for f in out_dir.iterdir():
        stem = f.name.split(".")[0]
        if f.suffix in {".mp4", ".jpg", ".ass", ".json"} and f.name != "manifest.json" and stem not in keep_stems:
            f.unlink()


def build_manifest(info, transcript, clips, results, llm: dict, cfg, rules, url, rendered: bool) -> dict:
    """`llm` is {"model", "usage"} describing whoever chose the clips."""
    vid = info["id"]
    items = []
    for c in clips:
        r = results.get(c["id"], {})
        a = r.get("analysis") or {}
        items.append({
            "id": c["id"],
            "status": r.get("status", "candidate" if not rendered else "failed"),
            **({"error": r["error"]} if r.get("error") else {}),
            "file": r.get("file"),
            "thumbnail": r.get("thumbnail"),
            "title": c["title"],
            "hook_line": c["hook_line"],
            "reason": c["reason"],
            "score": c["score"],
            "score_breakdown": c["score_breakdown"],
            "heatmap": c["heatmap"],
            "selected_by": c["selected_by"],
            "start_sec": c["start_sec"],
            "end_sec": c["end_sec"],
            "start": hms(c["start_sec"]),
            "end": hms(c["end_sec"]),
            "duration_sec": r.get("duration_sec", round(c["end_sec"] - c["start_sec"], 3)),
            "source_t0": r.get("source_t0"),
            "source_url": "" if info.get("local") else f"https://youtu.be/{vid}?t={int(c['start_sec'])}",
            "transcript": c["text"],
            "reframe": {
                "detector": a.get("detector"),
                "face_coverage": a.get("face_coverage"),
                "shots": len(a.get("shots", [])),
                "source_resolution": f"{a['width']}x{a['height']}" if a else None,
            } if a else None,
            "caption_words": r.get("caption_words"),
            "review": {"approved": None, "notes": "", "marks": [], "trims": []},
        })
    return {
        "tool": f"clipper {__version__}",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "video": {
            "id": vid,
            "url": url,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration_sec": info.get("duration"),
            "upload_date": info.get("upload_date"),
        },
        "transcript": {
            "subtitle_source": transcript["source"],
            "subtitle_lang": transcript["lang"],
            "word_timings": transcript["has_word_timings"],
            "heatmap_available": bool(transcript["heatmap"]),
        },
        "llm": llm,
        "campaign_rules": rules or None,
        "settings": {"num_clips": cfg["num_clips"], "clips": cfg["clips"], "scoring": cfg["scoring"]},
        "clips": items,
    }


def review_html(m: dict) -> str:
    e = html.escape
    cards = []
    for c in m["clips"]:
        bd = " · ".join(f"{k.replace('_', ' ')} {v:g}" for k, v in c["score_breakdown"].items())
        media = (
            f'<video controls preload="metadata" src="{e(c["file"])}"'
            + (f' poster="{e(c["thumbnail"])}"' if c.get("thumbnail") else "") + "></video>"
            if c.get("file") else f'<div class="err">{e(c.get("error", c["status"]))}</div>'
        )
        cards.append(f"""<article>
{media}
<div class="body">
<h2>#{c['id']} {e(c['title'])}</h2>
<p class="meta"><b>{c['score']:.2f}</b> · {c['duration_sec']:.0f}s · <a href="{e(c['source_url'])}" target="_blank">{e(c['start'][:8])}–{e(c['end'][:8])}</a> · {e(c['selected_by'])}{' · 🔥 replayed' if c['heatmap'] >= 0.5 else ''}</p>
<p class="hook">“{e(c['hook_line'])}”</p>
<p>{e(c['reason'])}</p>
<p class="small">{e(bd)}</p>
<details><summary>Transcript</summary><p class="small">{e(c['transcript'])}</p></details>
</div></article>""")
    v = m["video"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review</title>
<style>
:root{{--bg:#f6f6f4;--card:#fff;--fg:#1b1b1b;--muted:#666;--line:#e3e3e0;--accent:#d14b1f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141414;--card:#1e1e1e;--fg:#eee;--muted:#9a9a9a;--line:#2c2c2c;--accent:#ff7a4d}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
header{{padding:20px 16px;max-width:1200px;margin:auto}} h1{{font-size:20px;margin:0 0 4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:0 16px 32px;max-width:1200px;margin:auto}}
article{{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
video{{width:100%;aspect-ratio:9/16;background:#000;display:block}}
.body{{padding:12px 14px}} h2{{font-size:16px;margin:0 0 4px}} .meta,.small{{color:var(--muted);font-size:13px}}
.hook{{font-style:italic}} a{{color:var(--accent)}} .err{{padding:40px 14px;color:var(--accent)}}
</style></head><body>
<header><h1>{e(v['title'] or v['id'])}</h1>
<div class="meta">{e(v.get('channel') or '')} · {len(m['clips'])} clips · {e(m['transcript']['subtitle_source'])} captions · {e(m['llm']['model'])}</div></header>
<main class="grid">{''.join(cards)}</main></body></html>"""
