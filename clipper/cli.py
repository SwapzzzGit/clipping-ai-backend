"""Command-line entry point: `clipper <youtube-url> [options]`."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from clipper.config import load_config
from clipper.pipeline import STEPS, run
from clipper.util import ClipperError


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clipper", description="Turn a YouTube podcast into vertical short-form clips.")
    p.add_argument("url", help="YouTube video URL or 11-character video id")
    p.add_argument("-n", "--num-clips", type=int, help="number of clips to produce (default from config: 5)")
    p.add_argument("-c", "--config", default="config.yaml", help="config file (default: config.yaml)")
    p.add_argument("--rules", default="campaign_rules.txt", help="campaign rules file (default: campaign_rules.txt)")
    p.add_argument("--no-rules", action="store_true", help="ignore the campaign rules file")
    p.add_argument("--lang", help="subtitle language code (default from config: en)")
    p.add_argument("--provider", help="LLM provider: gemini | anthropic | openai | mock (overrides LLM_PROVIDER)")
    p.add_argument("--model", help="LLM model id (overrides LLM_MODEL)")
    p.add_argument("--min-sec", type=float, help="minimum clip length")
    p.add_argument("--max-sec", type=float, help="maximum clip length")
    p.add_argument("--no-captions", action="store_true", help="do not burn in captions")
    p.add_argument("--work-dir", help="cache directory (default: work)")
    p.add_argument("--output-dir", help="output directory (default: output)")
    p.add_argument("--candidates-only", action="store_true",
                   help="stop after choosing clips (no video download); writes manifest.json for review")
    p.add_argument("--force", nargs="?", const="transcript", choices=STEPS, metavar="STEP",
                   help=f"ignore caches from STEP onward ({', '.join(STEPS)}); bare --force redoes everything")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    os.environ.setdefault("GLOG_minloglevel", "2")  # quiet MediaPipe's C++ logging
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.model:
        os.environ["LLM_MODEL"] = args.model

    overrides: dict = {}
    if args.num_clips:
        overrides["num_clips"] = args.num_clips
    if args.lang:
        overrides["language"] = args.lang
    if args.work_dir:
        overrides["work_dir"] = args.work_dir
    if args.output_dir:
        overrides["output_dir"] = args.output_dir
    if args.no_captions:
        overrides["render"] = {"captions": False}
    clip_len = {k: v for k, v in (("min_sec", args.min_sec), ("max_sec", args.max_sec)) if v}
    if clip_len:
        overrides["clips"] = clip_len

    cfg = load_config(args.config, overrides)
    if cfg["clips"]["min_sec"] >= cfg["clips"]["max_sec"]:
        print("error: min clip length must be below max clip length", file=sys.stderr)
        return 2
    try:
        out = run(args.url, cfg, None if args.no_rules else args.rules, args.force, args.candidates_only)
    except ClipperError as e:
        logging.getLogger("clipper").error("%s", e)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; re-run the same command to resume from cache", file=sys.stderr)
        return 130
    print(out.resolve() / "manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
