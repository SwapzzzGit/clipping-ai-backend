"""Hand the "where are the best moments?" step to Claude, and take its answer back.

For each job in Claude mode the worker writes a self-contained task folder:

    backend/data/claude_tasks/<job_id>/
        task.md          instructions + output format
        transcript.txt   "[seconds] text" lines
        heatmap.txt      YouTube "Most replayed" peaks (if available)
        selection.json   <- Claude writes this; the worker picks it up and continues

Claude can pick a task up three ways: the backend launches Claude Code headless
(`claude -p`), someone runs the `/clip-jobs` command in a Claude Code session in this
repo, or a scheduled routine runs that command. All three just write selection.json.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.store import TASKS_DIR
from clipper.candidates import SENTENCE_END, split_rules
from clipper.util import hms, mmss, write_json

TASK_PROMPT = (
    "You are processing a Clipper Studio job. Read task.md in the current directory and follow it exactly. "
    "Read transcript.txt (and heatmap.txt if present), choose the clips, then write selection.json in the "
    "current directory. Do not run shell commands or edit any other file."
)


def task_dir(job_id: str) -> Path:
    return TASKS_DIR / job_id


def transcript_lines(segments: list[dict], max_words: int = 30) -> list[str]:
    """Merge caption fragments into sentence-sized lines, each prefixed with its start second."""
    lines, cur, start = [], [], None
    for s in segments:
        if start is None:
            start = s["start_sec"]
        cur.append(s["text"])
        words = sum(len(x.split()) for x in cur)
        if SENTENCE_END.search(s["text"]) or words >= max_words:
            lines.append(f"[{start:.1f}] {' '.join(cur)}")
            cur, start = [], None
    if cur:
        lines.append(f"[{start:.1f}] {' '.join(cur)}")
    return lines


def write_task(job: dict, info: dict, transcript: dict, cfg: dict, rules: str) -> Path:
    d = task_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "selection.json").unlink(missing_ok=True)
    (d / "transcript.txt").write_text("\n".join(transcript_lines(transcript["segments"])) + "\n", encoding="utf-8")
    peaks = sorted((h for h in transcript["heatmap"] if h["value"] >= 0.5), key=lambda h: -h["value"])[:20]
    if peaks:
        (d / "heatmap.txt").write_text(
            "\n".join(f"{h['start_sec']:.0f}-{h['end_sec']:.0f}s ({mmss(h['start_sec'])}) replay intensity {h['value']:.2f}"
                      for h in sorted(peaks, key=lambda h: h["start_sec"])) + "\n", encoding="utf-8")
    n, mn, mx = cfg["num_clips"], cfg["clips"]["min_sec"], cfg["clips"]["max_sec"]
    if cfg.get("auto_clips"):
        pick = (f"Pick **up to {n}** moments from this video that would plausibly go viral as vertical short-form clips "
                "(TikTok / Reels / Shorts). Fewer strong clips beat weak ones, but give at least one, and score strictly "
                "(7+ only for clips worth posting).")
        main_picks = "main picks"
    else:
        pick = (f"Pick the **{n}** moments from this video most likely to perform as vertical short-form clips "
                "(TikTok / Reels / Shorts).")
        main_picks = f"{n} main picks"
    hard, preferred = split_rules(rules or "")
    rules_md = (f"\n## Campaign rules (HARD constraints)\n\n{hard}\n" if hard else "") + (
        f"\n## Preferences (weigh these when choosing between good moments)\n\n{preferred}\n" if preferred else "")
    heat_md = ("- `heatmap.txt`: YouTube \"Most replayed\" peaks. Treat them as a tie-breaker, not a requirement.\n"
               if peaks else "")
    (d / "task.md").write_text(f"""# Clip selection task: job {job['id']}

**Video:** {info.get('title')} ({info.get('channel') or info.get('uploader') or 'unknown channel'})
**URL:** {job['url']}
**Duration:** {hms(info.get('duration') or 0)[:8]}

## Your job

{pick} Everything else (downloading, face-tracked 9:16 cropping, captions)
is done by code after you answer, so only choose the moments.

Judge each candidate on:
- **hook_strength**: would the first sentence stop someone scrolling?
- **emotion**: funny, angry, moving, inspiring, shocking
- **suspense_or_curiosity**: opens a question the viewer needs answered
- **controversy_or_surprise**: hot take, counter-intuitive claim, surprising fact
- **standalone_clarity**: makes sense with zero prior context (no "as I said", unexplained names)

Rules for every clip:
- {mn}-{mx} seconds long; clips must not overlap
- starts on a strong hook sentence (not "and", "so", "but yeah")
- ends right after a complete thought, never mid-sentence
- `hook_line` must be the clip's first sentence copied **verbatim** from the transcript; the code
  uses it to find the exact start word
- `music_mood`: the background-music mood that suits the clip: calm, upbeat, tense, emotional, serious or funny
{rules_md}
## Inputs

- `transcript.txt`: one line per sentence, `[start_seconds] text`. Clip times are in seconds.
{heat_md}
## Output

Write `selection.json` in this folder, exactly this shape, best clip first. You may add up to 2
extra alternates after the {main_picks}; they are used only if a main pick turns out invalid.

```json
{{
  "clips": [
    {{
      "start_sec": 1234.5,
      "end_sec": 1271.0,
      "title": "Punchy title, max 60 characters",
      "hook_line": "Exact first sentence of the clip.",
      "reason": "One line on why this will perform.",
      "music_mood": "calm",
      "score": 8.5,
      "score_breakdown": {{"hook_strength": 9, "emotion": 7, "suspense_or_curiosity": 8,
                          "controversy_or_surprise": 6, "standalone_clarity": 9}}
    }}
  ]
}}
```

Write only `selection.json`. Do not modify other files.
""", encoding="utf-8")
    return d


def find_claude(configured: str = "") -> str | None:
    """Explicit setting, then PATH, then the CLI bundled with the Claude desktop app (newest)."""
    if configured:
        return configured if (Path(configured).is_file() or shutil.which(configured)) else None
    found = shutil.which("claude")
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        bundled = glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "*", "claude.exe"))
        if bundled:
            return max(bundled, key=lambda p: _version_key(Path(p).parts[-3]))
    return None


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", v))


def claude_version(cmd: str) -> str | None:
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30, env=_clean_env())
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _clean_env() -> dict:
    # A Claude Code session that started this server sets these; a child `claude` would
    # otherwise think it is nested inside that session.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    return env


def run_claude(job_id: str, cmd: str, model: str = "", timeout_min: int = 20) -> tuple[bool, str]:
    """Run Claude Code headless in the task folder. Returns (ok, output tail)."""
    d = task_dir(job_id)
    args = [cmd, "-p", TASK_PROMPT, "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Write", "Glob", "--output-format", "text"]
    if model:
        args += ["--model", model]
    try:
        proc = subprocess.run(args, cwd=d, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_min * 60, env=_clean_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"Claude did not finish within {timeout_min} minutes"
    except OSError as e:
        return False, f"Could not start Claude Code: {e}"
    out = (proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")).strip()
    if "not logged in" in out.lower() or "/login" in out:
        return False, (f"{out}\n\nClaude Code is installed but not signed in. Open a terminal, run\n"
                       f'  "{cmd}"\nonce and use /login. Until then, use /clip-jobs in a Claude session.')
    ok = proc.returncode == 0 and (d / "selection.json").is_file()
    if proc.returncode == 0 and not ok:
        out += "\n(Claude exited without writing selection.json)"
    return ok, out[-2000:]


def save_selection(job_id: str, selection: dict) -> None:
    """Used when the selection arrives over HTTP instead of as a file."""
    write_json(task_dir(job_id) / "selection.json", selection)
