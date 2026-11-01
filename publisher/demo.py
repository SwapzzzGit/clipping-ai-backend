"""Offline end-to-end demo: connect 3 mock accounts, publish 2 clips to all of them in parallel.

    python -m publisher.demo [source]      # source: a video/job id under output/, or a folder

Uses a throwaway data dir, so it never touches real accounts or the real publish store.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from publisher import REPO_ROOT


def pick_source(arg: str | None) -> str:
    if arg:
        return arg
    found = sorted(REPO_ROOT.glob("output/*/manifest.json")) + sorted(REPO_ROOT.glob("backend/data/output/*/manifest.json"))
    if not found:
        sys.exit("No rendered clips found; run clipper first or pass a folder with manifest.json")
    return str(found[0].parent)


def main(argv: list[str]) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="publisher-demo-"))
    os.environ["PUBLISHER_DATA"] = str(tmp)
    os.environ.setdefault("PUBLISHER_MOCK_DELAY", "1.0")
    from publisher.connectors.mock import MockConnector
    from publisher.service import Publisher

    source = pick_source(argv[0] if argv else None)
    pub = Publisher(data_dir=tmp, connectors={"mock": MockConnector()}, url_provider=None, ffmpeg="")
    accounts = []
    for handle in ("podcast_main", "podcast_clips", "brand_shorts"):
        url = urllib.parse.urlparse(pub.start_oauth("mock", login_hint=handle))
        q = dict(urllib.parse.parse_qsl(url.query))
        acct, _ = pub.finish_oauth("mock", q["code"], q["state"])
        accounts.append(acct)
        print(f"connected  {acct['id']:<16} @{acct['username']}")

    variants = pub.preview(source, [a["id"] for a in accounts])
    clip_ids = sorted({v["clip_id"] for v in variants})[:2]
    job = pub.create_job(source, account_ids=[a["id"] for a in accounts], clip_ids=clip_ids)
    print(f"job {job['id']}: {len(job['targets'])} targets (clips {clip_ids} x {len(accounts)} accounts)")
    t0 = time.monotonic()
    pub.start()
    seen = {}
    while True:
        j = pub.jobs.get(job["id"])
        for t in j["targets"]:
            if seen.get(t["id"]) != t["status"]:
                seen[t["id"]] = t["status"]
                print(f"  +{time.monotonic() - t0:5.2f}s  {t['id']} clip {t['clip_id']} -> "
                      f"{t['account_name']:<20} {t['status']}")
        if j["status"] in ("published", "failed", "partial", "cancelled"):
            break
        time.sleep(0.05)
    pub.stop()
    elapsed = time.monotonic() - t0
    serial = len(j["targets"]) * float(os.environ["PUBLISHER_MOCK_DELAY"])
    print(f"\njob {j['status']} in {elapsed:.1f}s (one at a time would take >= {serial:.1f}s)")
    for t in j["targets"]:
        print(f"  clip {t['clip_id']}  {t['account_name']:<20} {t['url']}")
    print(f"\ndemo data (mock posts, jobs, encrypted accounts): {tmp}")
    return 0 if j["status"] == "published" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
