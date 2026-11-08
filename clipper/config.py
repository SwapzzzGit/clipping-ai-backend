"""Config loading: built-in defaults <- config.yaml <- CLI overrides."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "work_dir": "work",
    "output_dir": "output",
    "language": "en",
    "num_clips": 5,
    "auto_clips": False,      # True: num_clips is a cap and only clips scoring >= auto_min_score are kept
    "auto_min_score": 7.0,
    "ffmpeg_path": None,
    "llm": {
        "provider": "gemini",
        "model": None,
        "base_url": None,
        "temperature": 0.2,
        "max_tokens": 8192,
        "concurrency": 4,
        "timeout_sec": 120,
    },
    "windows": {"length_sec": 75, "stride_sec": 30, "batch_size": 6},
    "scoring": {
        "weights": {
            "hook_strength": 0.30,
            "emotion": 0.15,
            "suspense_or_curiosity": 0.20,
            "controversy_or_surprise": 0.15,
            "standalone_clarity": 0.20,
        },
        "heatmap_bonus": 1.0,
        "heatmap_threshold": 0.5,
        "top_k": 15,
    },
    "clips": {"min_sec": 20, "max_sec": 60, "snap_tolerance_sec": 4},
    "transcribe": {"model": "small", "device": "cpu", "compute_type": "int8"},
    "download": {"max_height": 1080, "padding_sec": 0.5, "parallel": 3, "retries": 2},
    "render": {
        "width": 1080,
        "height": 1920,
        "crf": 20,
        "preset": "veryfast",
        "face_sample_fps": 3,
        "shot_change_threshold": 0.5,
        "captions": True,
        "source_captions": "auto",   # auto | off: captions already in the picture
        "caption_words_per_line": 3,
        "caption_font": "Arial",
        "caption_font_size": 84,
        "caption_highlight": "&H0000E5FF",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = "config.yaml", overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path and Path(path).is_file():
        with open(path, encoding="utf-8") as f:
            cfg = _merge(cfg, yaml.safe_load(f) or {})
    return _merge(cfg, overrides or {})


def load_campaign_rules(path: str | Path | None) -> str:
    """Return the rules text with comment and blank lines removed ('' if none)."""
    if not path or not Path(path).is_file():
        return ""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return "\n".join(l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#"))
