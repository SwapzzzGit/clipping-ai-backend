"""Per-platform post variants of each clip, built from the clip's manifest.json fields.

`build_variants` gives one editable PostSpec per (clip x target account). Copy is templated from
title / transcript / source_url by default, or written by the LLM through `clipper.llm.LLM` (one
call per clip covering every platform involved). Platform limits are enforced afterwards either way.
`prepare_video` trims/re-encodes a clip that breaks a platform's duration or size limit.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from clipper.util import extract_json, read_json
from publisher import config
from publisher.connectors.base import Connector
from publisher.models import Limits, PostSpec

STOP = set("""about above after again against all also because been before being below between both but
could does doing down during each from further have having here hers herself himself into itself just more
most myself once only other ought ours ourselves over same should some such than that their theirs them
themselves then there these they this those through under until very what when where which while whom
with would your yours yourself yourselves why how really things thing never always every""".split())


# -- clip sources ----------------------------------------------------------------------------

def find_source(source: str) -> Path:
    """Folder holding `<source>/manifest.json` (a backend job id or a CLI video id), or a path."""
    p = Path(source)
    if (p / "manifest.json").is_file():
        return p.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source):
        raise FileNotFoundError(f"No clips found for {source!r}")
    for root in config.output_roots():
        if (root / source / "manifest.json").is_file():
            return (root / source).resolve()
    raise FileNotFoundError(f"No manifest.json for {source!r} in {', '.join(map(str, config.output_roots()))}")


def load_clips(source: str, clip_ids: list[int] | None = None) -> tuple[Path, dict, list[dict]]:
    folder = find_source(source)
    manifest = read_json(folder / "manifest.json")
    clips = [c for c in manifest.get("clips", []) if c.get("status", "ok") == "ok"
             and (clip_ids is None or c["id"] in clip_ids)]
    return folder, manifest, clips


# -- copy ------------------------------------------------------------------------------------

def hashtagify(word: str) -> str:
    return "#" + "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", word))


def derive_hashtags(clip: dict, n: int = 3) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{4,}", clip.get("title", ""))
    picked = []
    for w in words:
        w = w.strip("'-")
        if w.lower() not in STOP and hashtagify(w).lower() not in (t.lower() for t in picked):
            picked.append(hashtagify(w))
    return picked[:n]


def teaser(clip: dict, max_chars: int = 180) -> str:
    """First sentence or two of the clip's transcript, cut at a word boundary."""
    text = re.sub(r"\s+", " ", clip.get("transcript") or clip.get("hook_line") or "").strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sentences:
        if len(out) + len(s) + 1 > max_chars:
            break
        out = f"{out} {s}".strip()
    if not out:
        out = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return out


def merge_tags(*groups: list[str], limit: int | None = None) -> list[str]:
    seen, out = set(), []
    for g in groups:
        for t in g or []:
            t = t if t.startswith("#") else hashtagify(t)
            if len(t) > 1 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out[:limit] if limit else out


def template_copy(platform: str, clip: dict, manifest: dict) -> dict:
    title = clip.get("title") or "Clip"
    quote = teaser(clip)
    video = manifest.get("video") or {}
    credit = f"From: {video.get('title')} ({video.get('channel')})" if video.get("title") else ""
    link = clip.get("source_url") or video.get("url") or ""
    if platform == "youtube":
        body = "\n\n".join(x for x in (f"“{quote}”" if quote else "", credit, f"Full episode: {link}" if link else "") if x)
        return {"title": title, "caption": body}
    if platform == "instagram":   # links are not clickable in captions, so no URL
        return {"title": title, "caption": "\n\n".join(x for x in (title, f"“{quote}”" if quote else "", credit) if x)}
    # tiktok and anything else: short and punchy
    return {"title": title, "caption": f"{title}\n\n{credit}".strip()}


LLM_SYSTEM = """You write social media copy for short vertical video clips cut from long podcasts.
Return only JSON: {"<platform>": {"title": str, "caption": str, "hashtags": [str]}} for each platform asked.
Rules: hook the viewer in the first line; no clickbait lies; no emojis spam (max 2); hashtags are
"#CamelCase" without spaces; youtube title <= 90 chars; captions <= 400 chars; 3-6 hashtags each.
Credit the podcast in the caption. Instagram captions must not contain URLs."""


def llm_copy(llm, clip: dict, manifest: dict, platforms: list[str]) -> dict:
    video = manifest.get("video") or {}
    user = json.dumps({"platforms": platforms, "podcast": video.get("title"), "channel": video.get("channel"),
                       "source_url": clip.get("source_url"), "clip_title": clip.get("title"),
                       "why_it_works": clip.get("reason"), "transcript": (clip.get("transcript") or "")[:2500]},
                      ensure_ascii=False)
    fallback = lambda: json.dumps({p: {**template_copy(p, clip, manifest), "hashtags": derive_hashtags(clip)}
                                   for p in platforms})
    try:
        data = extract_json(llm.complete(LLM_SYSTEM, user, fallback=fallback))
    except Exception:
        data = json.loads(fallback())
    return {p: data.get(p) or {} for p in platforms}


def fit_text(post: PostSpec, limits: Limits) -> PostSpec:
    """Clamp a PostSpec to a platform's limits (hashtag count, title and caption length)."""
    post.hashtags = merge_tags(limits.required_hashtags, post.hashtags, limit=limits.hashtag_max)
    if limits.title_max:
        post.title = post.title.strip()[: limits.title_max]
    if limits.privacy_options and post.privacy not in limits.privacy_options:
        post.privacy = limits.privacy_options[0] if "public" not in limits.privacy_options else "public"
    tag_len = len(" ".join(post.hashtags)) + 2
    room = limits.caption_max - tag_len
    if len(post.caption) > room:
        post.caption = post.caption[: max(0, room - 1)].rsplit(" ", 1)[0] + "…"
    return post


def build_variant(clip: dict, manifest: dict, folder: Path, account: dict, connector: Connector,
                  copy: dict | None = None) -> dict:
    platform = account["platform"]
    limits = connector.limits
    c = copy or template_copy(platform, clip, manifest)
    opts = account.get("options") or {}
    post = PostSpec(title=c.get("title") or clip.get("title", ""), caption=c.get("caption", ""),
                    hashtags=merge_tags(c.get("hashtags") or derive_hashtags(clip), opts.get("hashtags") or []),
                    privacy=opts.get("privacy", "public"),
                    options={"duration_sec": clip.get("duration_sec"), **(opts.get("post_options") or {})})
    if platform == "tiktok" and limits.private_only_unaudited and not getattr(connector, "audited", True):
        post.privacy = "private"
    fit_text(post, limits)
    video = folder / clip["file"]
    return {"clip_id": clip["id"], "clip_title": clip.get("title"), "clip_file": str(video),
            "duration_sec": clip.get("duration_sec"), "account_id": account["id"], "platform": platform,
            "account_name": account.get("display_name") or account.get("username"),
            "post": post.to_dict(), "issues": fit_issues(clip, video, limits)}


def build_variants(source: str, accounts: list[dict], connectors: dict[str, Connector],
                   clip_ids: list[int] | None = None, llm=None) -> list[dict]:
    folder, manifest, clips = load_clips(source, clip_ids)
    platforms = sorted({a["platform"] for a in accounts})
    out = []
    for clip in clips:
        copies = llm_copy(llm, clip, manifest, platforms) if llm else {}
        for acct in accounts:
            out.append(build_variant(clip, manifest, folder, acct, connectors[acct["platform"]],
                                     copies.get(acct["platform"])))
    return out


# -- video fixes -----------------------------------------------------------------------------

def fit_issues(clip: dict, video: Path, limits: Limits) -> list[str]:
    issues = []
    dur = clip.get("duration_sec") or 0
    if dur > limits.max_duration_sec:
        issues.append(f"{dur:.0f}s is over the {limits.max_duration_sec:.0f}s limit: will be trimmed")
    if dur and dur < limits.min_duration_sec:
        issues.append(f"{dur:.1f}s is under the {limits.min_duration_sec:.0f}s minimum")
    if limits.max_file_mb and video.is_file() and video.stat().st_size > limits.max_file_mb * 1024 * 1024:
        issues.append(f"file is over {limits.max_file_mb:.0f} MB: will be re-encoded")
    if not video.is_file():
        issues.append("video file is missing")
    return issues


def prepare_video(src: Path, duration: float | None, limits: Limits, cache_dir: Path, ffmpeg: str | None) -> Path:
    """Return `src`, or a trimmed / re-encoded copy that fits `limits` (cached by content)."""
    size_mb = src.stat().st_size / 1024 / 1024
    too_long = bool(duration and duration > limits.max_duration_sec)
    too_big = bool(limits.max_file_mb and size_mb > limits.max_file_mb)
    if not (too_long or too_big):
        return src
    if not ffmpeg:
        raise RuntimeError("ffmpeg is needed to fit this clip to the platform limits")
    t = min(duration or limits.max_duration_sec, limits.max_duration_sec) - 0.05
    key = hashlib.sha1(f"{src.resolve()}|{src.stat().st_mtime}|{t}|{limits.max_file_mb}".encode()).hexdigest()[:12]
    dst = cache_dir / f"{src.stem}.{key}.mp4"
    if dst.is_file():
        return dst
    cache_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-t", f"{t:.2f}",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart"]
    if too_big:
        kbps = int(limits.max_file_mb * 8 * 1024 * 0.9 / t) - 128
        cmd += ["-b:v", f"{max(kbps, 300)}k", "-maxrate", f"{max(kbps, 300)}k", "-bufsize", f"{2 * max(kbps, 300)}k"]
    else:
        cmd += ["-crf", "20"]
    tmp = dst.with_suffix(".tmp.mp4")
    subprocess.run(cmd + [str(tmp)], check=True, capture_output=True)
    tmp.replace(dst)
    return dst
